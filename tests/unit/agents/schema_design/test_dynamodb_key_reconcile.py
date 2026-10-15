"""Shared tables that parallel groups gave different keys (issue #772).

DynamoDB schema design always runs in groups. When two groups design the same
source table with different key schemas, GSIs or key-attribute names, the
merge reconciles them deterministically before it combines the drafts: one
key schema per table, the other groups' keys kept as GSIs, so every group's
access patterns are still served. Only what cannot be reconciled is left as a
merge failure for the fix round. No model is called anywhere here.
"""

from __future__ import annotations

import json

from src.agents.schema_design.dynamodb_key_reconcile import GSI_LIMIT
from src.agents.schema_design.group_merger import (
    merge_failures,
    merge_group_drafts,
    merge_warnings,
)
from src.contracts.dynamodb_model_output import AccessPattern, TableDefinition

ORDERS = "shop.orders"
# The source schema's unique keys (collector primary keys / unique indexes).
UNIQUE = {"orders": [frozenset({"order_id"})]}


def _key(name: str, type_: str = "N") -> dict:
    return {"attribute_name": name, "attribute_type": type_}


def _attr(name: str, source_table: str, column: str | None = None, type_: str = "N") -> dict:
    return {
        "name": name,
        "type": type_,
        "source_table": source_table,
        "source_column": column or name,
    }


def _gsi(name: str, pk: list[str], sk: list[str] | None = None, projection: str = "ALL") -> dict:
    return {
        "gsi_name": name,
        "partition_key": [_key(k) for k in pk],
        "sort_key": [_key(k) for k in sk] if sk else None,
        "projection": projection,
        "item_count": 10,
        "item_size_bytes": 100,
    }


def _single(
    name: str,
    source: str,
    pk: str,
    sk: str | None = None,
    attrs: list[dict] | None = None,
    gsis: list[dict] | None = None,
) -> dict:
    return {
        "table_name": name,
        "aggregate_pattern": "separate",
        "source_tables": [source],
        "partition_key": _key(pk),
        "sort_key": _key(sk) if sk else None,
        "attributes": attrs or [_attr(pk, source)] + ([_attr(sk, source)] if sk else []),
        "gsis": gsis or [],
        "item_count": 10,
        "item_size_bytes": 100,
    }


def _entity(entity_type: str, source: str, pk: str, sk: str, *attrs: dict) -> dict:
    return {
        "entity_type": entity_type,
        "source_table": source,
        "pk_template": pk,
        "sk_template": sk,
        "attributes": list(attrs) or [_attr("id", source)],
    }


def _collection(name: str, pk: str, sk: str, *entities: dict, gsis=()) -> dict:
    return {
        "table_name": name,
        "aggregate_pattern": "item_collection",
        "source_tables": list(dict.fromkeys(e["source_table"] for e in entities)),
        "partition_key": _key(pk),
        "sort_key": _key(sk, "S"),
        "entities": list(entities),
        "gsis": list(gsis),
        "item_count": 10,
        "item_size_bytes": 100,
    }


def _ap(
    pid: str,
    table: str,
    operation: str,
    key_condition: str,
    *,
    gsi: str | None = None,
    source: str = ORDERS,
    consistent: bool = False,
) -> dict:
    return {
        "pattern_id": pid,
        "pattern_group": "g",
        "query_ids": [f"q-{pid}"],
        "source_tables": [source],
        "description": "d",
        "operation": operation,
        "table_name": table,
        "gsi_name": gsi,
        "key_condition": key_condition,
        "design_rps": 1.0,
        "item_size_bytes": 100,
        "strongly_consistent": consistent,
    }


def _draft(tables: list[dict], aps: list[dict], trade_offs: list[dict] | None = None) -> dict:
    return {
        "table_definitions": tables,
        "access_patterns": aps,
        "hot_partition_analysis": [],
        "trade_offs": trade_offs or [],
        "validation_passed": True,
        "validation_failures": [],
    }


def _pattern(merged: dict, operation: str) -> dict:
    """The one merged access pattern with ``operation`` (draft order is not the merge order)."""
    found: list[dict] = [ap for ap in merged["access_patterns"] if ap["operation"] == operation]
    assert len(found) == 1, found
    return found[0]


def _by_name(merged: dict) -> dict[str, dict]:
    return {t["table_name"]: t for t in merged["table_definitions"]}


def _assert_every_pattern_is_served(merged: dict) -> None:
    """Each access pattern names a merged table, and a GSI that table has.

    A read on a GSI must be a Query (GetItem/BatchGetItem cannot run on an
    index), and the merged design must still validate against the contract.
    """
    tables = _by_name(merged)
    for table in tables.values():
        TableDefinition.model_validate(table)
    for ap in merged["access_patterns"]:
        AccessPattern.model_validate(ap)
        assert ap["table_name"] in tables, ap
        gsi = ap.get("gsi_name")
        if gsi:
            names = {g["gsi_name"] for g in tables[ap["table_name"]]["gsis"]}
            assert gsi in names, (ap, names)
            assert ap["operation"] == "Query", ap


def _keys(table: dict) -> tuple:
    sk = table.get("sort_key")
    return table["partition_key"]["attribute_name"], sk["attribute_name"] if sk else None


def _gsi_keys(table: dict) -> dict[str, tuple]:
    return {
        g["gsi_name"]: (
            tuple(k["attribute_name"] for k in g["partition_key"]),
            tuple(k["attribute_name"] for k in g.get("sort_key") or []),
        )
        for g in table["gsis"]
    }


# ---------------------------------------------------------------------------
# Different key schemas for the same single-entity table
# ---------------------------------------------------------------------------


class TestDifferentKeySchemas:
    def _two_groups(self) -> list[dict]:
        """Group 0 keys orders by customer and date, group 1 by order id."""
        attrs = [
            _attr("order_id", ORDERS),
            _attr("customer_id", ORDERS),
            _attr("order_date", ORDERS, type_="S"),
        ]
        g0 = _draft(
            [_single("Orders", ORDERS, "customer_id", "order_date", attrs=attrs)],
            [_ap("DDB-AP-1", "Orders", "Query", "PK=customer_id AND SK begins_with :month")],
        )
        g0["table_definitions"][0]["sort_key"] = _key("order_date", "S")
        g1 = _draft(
            [_single("Orders", ORDERS, "order_id", attrs=attrs)],
            [
                _ap("DDB-AP-1", "Orders", "GetItem", "PK=order_id"),
                _ap("DDB-AP-2", "Orders", "UpdateItem", "PK=order_id (SET status)"),
            ],
        )
        return [g0, g1]

    def test_one_table_keeps_the_key_most_patterns_use_and_the_other_becomes_a_gsi(self):
        merged = merge_group_drafts(self._two_groups(), "dynamodb", unique_keys=UNIQUE)

        [orders] = merged["table_definitions"]
        assert _keys(orders) == ("order_id", None)
        assert (("customer_id",), ("order_date",)) in _gsi_keys(orders).values()
        assert merge_failures(merged) == []
        assert merged["validation_passed"] is True
        _assert_every_pattern_is_served(merged)

    def test_the_moved_patterns_query_the_new_gsi(self):
        merged = merge_group_drafts(self._two_groups(), "dynamodb", unique_keys=UNIQUE)

        [orders] = merged["table_definitions"]
        [gsi_name] = [
            n for n, k in _gsi_keys(orders).items() if k == (("customer_id",), ("order_date",))
        ]
        by_id = {ap["pattern_id"]: ap for ap in merged["access_patterns"]}
        moved = by_id["DDB-AP-1"]
        assert moved["gsi_name"] == gsi_name and moved["operation"] == "Query"
        assert gsi_name in moved["key_condition"]
        assert by_id["DDB-AP-2"]["gsi_name"] is None  # group 1's own GetItem stays on the table
        assert by_id["DDB-AP-3"]["gsi_name"] is None

    def test_a_trade_off_records_the_reconciliation(self):
        merged = merge_group_drafts(self._two_groups(), "dynamodb", unique_keys=UNIQUE)

        notes = [t for t in merged["trade_offs"] if ORDERS in t.get("source_tables", [])]
        assert len(notes) == 1
        assert "group 0" in notes[0]["description"] and "group 1" in notes[0]["description"]
        assert notes[0]["target_tables"] == ["Orders"]
        assert notes[0]["query_ids"] == ["q-DDB-AP-1"]

    def test_three_groups_three_sort_keys(self):
        """The WpWcTaxRateClasses shape: groups 0, 1 and 3 each pick their own sort key."""
        src = "wordpress.wp_wc_tax_rate_classes"
        attrs = [_attr(c, src, type_="S") for c in ("tax_rate_class_id", "name", "slug")]

        def table(sk: str | None) -> dict:
            t = _single("WpWcTaxRateClasses", src, "name", sk, attrs=attrs)
            t["partition_key"] = _key("name", "S")
            t["sort_key"] = _key(sk, "S") if sk else None
            return t

        drafts = [
            _draft(
                [table("slug")],
                [_ap("DDB-AP-1", "WpWcTaxRateClasses", "Query", "PK=name AND SK=slug", source=src)],
            ),
            _draft(
                [table("tax_rate_class_id")],
                [
                    _ap(
                        "DDB-AP-1",
                        "WpWcTaxRateClasses",
                        "GetItem",
                        "PK=name AND SK=tax_rate_class_id",
                        source=src,
                    ),
                    _ap(
                        "DDB-AP-2",
                        "WpWcTaxRateClasses",
                        "DeleteItem",
                        "PK=name AND SK=tax_rate_class_id",
                        source=src,
                    ),
                ],
            ),
            _draft([_single("Other", "wordpress.wp_options", "option_id")], []),
            _draft(
                [table(None)],
                [_ap("DDB-AP-1", "WpWcTaxRateClasses", "BatchGetItem", "PK=name", source=src)],
            ),
        ]

        unique = {"wp_wc_tax_rate_classes": [frozenset({"tax_rate_class_id"})]}
        merged = merge_group_drafts(drafts, "dynamodb", unique_keys=unique)

        tables = _by_name(merged)
        assert set(tables) == {"WpWcTaxRateClasses", "Other"}
        assert _keys(tables["WpWcTaxRateClasses"]) == ("name", "tax_rate_class_id")
        assert merge_failures(merged) == []
        _assert_every_pattern_is_served(merged)

    def test_strongly_consistent_read_that_would_move_to_a_gsi_stays_a_conflict(self):
        """A GSI cannot give a strong read, so the key it needs is not replaced."""
        drafts = self._two_groups()
        drafts[0]["access_patterns"][0]["strongly_consistent"] = True

        merged = merge_group_drafts(drafts, "dynamodb", unique_keys=UNIQUE)

        assert len(merge_failures(merged)) == 1
        query = next(ap for ap in merged["access_patterns"] if ap["pattern_id"] == "DDB-AP-1")
        assert query["strongly_consistent"] is True and query["gsi_name"] is None
        assert "merged design" not in query["key_condition"]

    def test_key_used_only_by_a_put_needs_no_gsi(self):
        """A PutItem writes the whole row, so it works under any unique key."""
        drafts = self._two_groups()
        drafts[0]["access_patterns"] = [_ap("DDB-AP-1", "Orders", "PutItem", "full order item")]

        merged = merge_group_drafts(drafts, "dynamodb", unique_keys=UNIQUE)

        [orders] = merged["table_definitions"]
        assert _keys(orders) == ("order_id", None)
        assert orders["gsis"] == []
        assert merge_failures(merged) == [] and merged["validation_passed"] is True
        _assert_every_pattern_is_served(merged)

    def test_single_item_operations_with_the_same_key_columns_need_no_gsi(self):
        """(group_id, user_id) vs (user_id, group_id): a delete knows both columns."""
        src = "discourse.group_users"
        attrs = [_attr("group_id", src), _attr("user_id", src)]
        g0 = _draft(
            [_single("group_users", src, "group_id", "user_id", attrs=attrs)],
            [
                _ap("DDB-AP-1", "group_users", "GetItem", "PK=group_id AND SK=user_id", source=src),
                _ap("DDB-AP-2", "group_users", "Query", "PK=group_id", source=src),
            ],
        )
        g1 = _draft(
            [_single("group_users", src, "user_id", "group_id", attrs=attrs)],
            [
                _ap(
                    "DDB-AP-1",
                    "group_users",
                    "DeleteItem",
                    "PK=user_id AND SK=group_id",
                    source=src,
                )
            ],
        )

        merged = merge_group_drafts([g0, g1], "dynamodb")

        [table] = merged["table_definitions"]
        assert _keys(table) == ("group_id", "user_id")
        assert table["gsis"] == []
        delete = _pattern(merged, "DeleteItem")
        assert delete["gsi_name"] is None and "PK group_id, SK user_id" in delete["key_condition"]

    def test_a_query_on_the_other_partition_key_needs_the_gsi(self):
        src = "discourse.group_users"
        attrs = [_attr("group_id", src), _attr("user_id", src)]
        g0 = _draft(
            [_single("group_users", src, "group_id", "user_id", attrs=attrs)],
            [_ap("DDB-AP-1", "group_users", "GetItem", "PK=group_id AND SK=user_id", source=src)],
        )
        g1 = _draft(
            [_single("group_users", src, "user_id", "group_id", attrs=attrs)],
            [_ap("DDB-AP-1", "group_users", "Query", "PK=user_id", source=src)],
        )

        merged = merge_group_drafts([g0, g1], "dynamodb")

        [table] = merged["table_definitions"]
        assert list(_gsi_keys(table).values()) == [(("user_id",), ("group_id",))]
        _assert_every_pattern_is_served(merged)

    def test_existing_gsi_with_the_same_keys_is_reused_and_widened(self):
        """A KEYS_ONLY GSI on those keys is reused; a read that now runs on it widens it."""
        drafts = self._two_groups()
        gsi = _gsi("OrdersByCustomer", ["customer_id"], ["order_date"], projection="KEYS_ONLY")
        gsi["sort_key"] = [_key("order_date", "S")]
        drafts[1]["table_definitions"][0]["gsis"] = [gsi]

        merged = merge_group_drafts(drafts, "dynamodb", unique_keys=UNIQUE)

        [orders] = merged["table_definitions"]
        assert [(g["gsi_name"], g["projection"]) for g in orders["gsis"]] == [
            ("OrdersByCustomer", "ALL")
        ]
        moved = next(ap for ap in merged["access_patterns"] if ap["pattern_id"] == "DDB-AP-1")
        assert moved["gsi_name"] == "OrdersByCustomer"

    def test_a_gsi_only_for_write_lookups_is_keys_only(self):
        """An update keyed by the replaced key only needs the table key back from the GSI."""
        drafts = self._two_groups()
        drafts[0]["access_patterns"] = [
            _ap("DDB-AP-1", "Orders", "UpdateItem", "PK=customer_id AND SK=order_date (SET x)")
        ]

        merged = merge_group_drafts(drafts, "dynamodb", unique_keys=UNIQUE)

        [orders] = merged["table_definitions"]
        [gsi] = orders["gsis"]
        assert gsi["projection"] == "KEYS_ONLY"
        assert gsi["item_size_bytes"] <= orders["item_size_bytes"]
        update = merged["access_patterns"][0]
        assert update["gsi_name"] is None and update["strongly_consistent"] is False
        assert gsi["gsi_name"] in update["key_condition"]
        [note] = [t for t in merged["trade_offs"] if ORDERS in t.get("source_tables", [])]
        assert "eventually consistent" in note["impact"] and "non-atomic" in note["impact"]
        assert "extra write" in note["impact"]


# ---------------------------------------------------------------------------
# Keys that would lose rows or guarantees stay a conflict (review of #803)
# ---------------------------------------------------------------------------

TOPIC_TAGS = "discourse.topic_tags"
USER_EMAILS = "discourse.user_emails"


class TestNoRowCollapse:
    def _topic_tags(self) -> list[dict]:
        """Group 0 keys topic_tags by topic (3 reads); group 1 by (topic, tag) and writes."""
        attrs = [_attr("topic_id", TOPIC_TAGS), _attr("tag_id", TOPIC_TAGS)]
        g0 = _draft(
            [_single("topic_tags", TOPIC_TAGS, "topic_id", attrs=attrs)],
            [
                _ap(f"DDB-AP-{i}", "topic_tags", "Query", "PK=topic_id", source=TOPIC_TAGS)
                for i in (1, 2, 3)
            ],
        )
        g1 = _draft(
            [_single("topic_tags", TOPIC_TAGS, "topic_id", "tag_id", attrs=attrs)],
            [
                _ap(
                    "DDB-AP-1",
                    "topic_tags",
                    "PutItem",
                    "PK=topic_id AND SK=tag_id",
                    source=TOPIC_TAGS,
                ),
                _ap(
                    "DDB-AP-2",
                    "topic_tags",
                    "DeleteItem",
                    "PK=topic_id AND SK=tag_id",
                    source=TOPIC_TAGS,
                ),
            ],
        )
        return [g0, g1]

    def test_a_key_with_fewer_columns_is_never_kept(self):
        """Keeping PK topic_id would make every tag PutItem overwrite the topic's last tag."""
        merged = merge_group_drafts(self._topic_tags(), "dynamodb")

        [table] = merged["table_definitions"]
        assert _keys(table) == ("topic_id", "tag_id")
        assert merge_failures(merged) == []
        _assert_every_pattern_is_served(merged)

    def test_the_wider_key_is_kept_even_when_the_narrower_has_more_patterns(self):
        drafts = self._topic_tags()
        drafts[0]["access_patterns"] += [
            _ap(f"DDB-AP-{i}", "topic_tags", "Query", "PK=topic_id", source=TOPIC_TAGS)
            for i in (4, 5, 6)
        ]

        merged = merge_group_drafts(drafts, "dynamodb")

        assert _keys(merged["table_definitions"][0]) == ("topic_id", "tag_id")

    def test_user_emails_without_schema_keeps_the_superset_key(self):
        """(user_id, user_email_id) vs (user_email_id): only the superset is safe unaided."""
        attrs = [_attr("user_id", USER_EMAILS), _attr("user_email_id", USER_EMAILS, "id")]
        g0 = _draft(
            [_single("user_emails", USER_EMAILS, "user_email_id", attrs=attrs)],
            [
                _ap("DDB-AP-1", "user_emails", "GetItem", "PK=user_email_id", source=USER_EMAILS),
                _ap("DDB-AP-2", "user_emails", "GetItem", "PK=user_email_id", source=USER_EMAILS),
            ],
        )
        g1 = _draft(
            [_single("user_emails", USER_EMAILS, "user_id", "user_email_id", attrs=attrs)],
            [_ap("DDB-AP-1", "user_emails", "Query", "PK=user_id", source=USER_EMAILS)],
        )

        merged = merge_group_drafts([g0, g1], "dynamodb")
        assert _keys(merged["table_definitions"][0]) == ("user_id", "user_email_id")
        _assert_every_pattern_is_served(merged)

        # With the source primary key (id), user_email_id alone is provably unique.
        unique = {"user_emails": [frozenset({"id"})]}
        merged = merge_group_drafts([g0, g1], "dynamodb", unique_keys=unique)
        assert _keys(merged["table_definitions"][0]) == ("user_email_id", None)
        assert merge_failures(merged) == []
        _assert_every_pattern_is_served(merged)

    def test_no_key_safe_for_every_group_stays_a_conflict(self):
        """(post_id) vs (topic_id, post_number) with no schema: neither holds the other."""
        src = "discourse.posts"
        attrs = [_attr("post_id", src, "id"), _attr("topic_id", src), _attr("post_number", src)]
        g0 = _draft(
            [_single("posts", src, "post_id", attrs=attrs)],
            [_ap("DDB-AP-1", "posts", "GetItem", "PK=post_id", source=src)],
        )
        g1 = _draft(
            [_single("posts", src, "topic_id", "post_number", attrs=attrs)],
            [_ap("DDB-AP-1", "posts", "Query", "PK=topic_id", source=src)],
        )

        merged = merge_group_drafts([g0, g1], "dynamodb")

        [failure] = merge_failures(merged)
        assert "'posts'" in failure
        assert merged["access_patterns"][1]["key_condition"] == "PK=topic_id"

    def test_a_conditional_write_keeps_its_key(self):
        """PK email with attribute_not_exists(email) is the uniqueness guarantee."""
        src = "shop.users"
        attrs = [_attr("user_id", src), _attr("email", src, type_="S")]
        g0 = _draft(
            [_single("users", src, "user_id", attrs=attrs)],
            [_ap("DDB-AP-1", "users", "GetItem", "PK=user_id", source=src) for _ in range(1)]
            + [_ap("DDB-AP-2", "users", "UpdateItem", "PK=user_id", source=src)],
        )
        g1_table = _single("users", src, "email", attrs=attrs)
        g1_table["partition_key"] = _key("email", "S")
        g1 = _draft(
            [g1_table],
            [
                _ap(
                    "DDB-AP-1",
                    "users",
                    "PutItem",
                    "PK=email, condition attribute_not_exists(email)",
                    source=src,
                )
            ],
        )
        unique = {"users": [frozenset({"user_id"}), frozenset({"email"})]}

        merged = merge_group_drafts([g0, g1], "dynamodb", unique_keys=unique)

        [table] = merged["table_definitions"]
        assert _keys(table) == ("email", None)  # user_id would lose the uniqueness check
        assert merge_failures(merged) == []
        put = _pattern(merged, "PutItem")
        assert put["key_condition"] == "PK=email, condition attribute_not_exists(email)"

    def test_conditional_writes_on_two_different_keys_stay_a_conflict(self):
        src = "shop.users"
        attrs = [_attr("user_id", src), _attr("email", src, type_="S")]
        g0 = _draft(
            [_single("users", src, "user_id", attrs=attrs)],
            [
                _ap(
                    "DDB-AP-1",
                    "users",
                    "PutItem",
                    "PK=user_id attribute_not_exists(user_id)",
                    source=src,
                )
            ],
        )
        g1_table = _single("users", src, "email", attrs=attrs)
        g1_table["partition_key"] = _key("email", "S")
        g1 = _draft(
            [g1_table],
            [
                _ap(
                    "DDB-AP-1",
                    "users",
                    "PutItem",
                    "PK=email attribute_not_exists(email)",
                    source=src,
                )
            ],
        )
        unique = {"users": [frozenset({"user_id"}), frozenset({"email"})]}

        merged = merge_group_drafts([g0, g1], "dynamodb", unique_keys=unique)

        assert len(merge_failures(merged)) == 1

    def test_a_transaction_on_the_replaced_key_stays_a_conflict(self):
        drafts = TestDifferentKeySchemas()._two_groups()
        drafts[0]["access_patterns"][0]["operation"] = "TransactWriteItems"

        merged = merge_group_drafts(drafts, "dynamodb", unique_keys=UNIQUE)

        assert len(merge_failures(merged)) == 1

    def test_key_type_disagreement_stays_a_failure(self):
        """N vs S for the same key attribute is never changed silently."""
        src = "wordpress.wp_users"
        a = _single("wp_users", src, "user_id")
        b = _single("wp_users", src, "user_id")
        b["partition_key"] = _key("user_id", "S")

        merged = merge_group_drafts([_draft([a], []), _draft([b], [])], "dynamodb")

        [failure] = merge_failures(merged)
        assert "key attribute types differ (user_id N vs S)" in failure
        assert merged["table_definitions"][0]["partition_key"]["attribute_type"] == "N"


class TestOrderAndNoise:
    def test_kept_key_does_not_depend_on_group_numbering(self):
        drafts = TestDifferentKeySchemas()._two_groups()
        drafts[0]["access_patterns"].append(
            _ap("DDB-AP-2", "Orders", "Query", "PK=customer_id")
        )  # 2 vs 2 patterns: a tie

        forward = merge_group_drafts(drafts, "dynamodb", unique_keys=UNIQUE)
        backward = merge_group_drafts(drafts[::-1], "dynamodb", unique_keys=UNIQUE)

        assert _keys(forward["table_definitions"][0]) == _keys(backward["table_definitions"][0])
        assert _keys(forward["table_definitions"][0]) == ("order_id", None)  # holds the primary key

    def test_one_trade_off_per_table_and_one_note_per_pattern(self):
        src = "wordpress.wp_wc_tax_rate_classes"
        attrs = [_attr(c, src, type_="S") for c in ("tax_rate_class_id", "name", "slug")]

        def table(sk: str | None) -> dict:
            t = _single("Classes", src, "tax_rate_class_id", sk, attrs=attrs)
            t["partition_key"] = _key("tax_rate_class_id", "S")
            t["sort_key"] = _key(sk, "S") if sk else None
            return t

        def other(pk: str) -> dict:
            t = _single("Classes", src, pk, attrs=attrs)
            t["partition_key"] = _key(pk, "S")
            return t

        drafts = [
            _draft(
                [table(None)],
                [_ap("DDB-AP-1", "Classes", "GetItem", "PK=tax_rate_class_id", source=src)] * 1,
            ),
            _draft(
                [other("slug")], [_ap("DDB-AP-1", "Classes", "BatchGetItem", "PK=slug", source=src)]
            ),
            _draft([other("name")], [_ap("DDB-AP-1", "Classes", "GetItem", "PK=name", source=src)]),
        ]
        unique = {"wp_wc_tax_rate_classes": [frozenset({"tax_rate_class_id"})]}
        merged = merge_group_drafts(drafts, "dynamodb", unique_keys=unique)

        trade_offs = [t for t in merged["trade_offs"] if src in t.get("source_tables", [])]
        assert len(trade_offs) == 1
        for ap in merged["access_patterns"]:
            assert ap["key_condition"].count("(merged design:") <= 1

    def test_mechanical_changes_go_to_the_notes_not_the_trade_offs(self):
        notes: list[str] = []
        src = "discourse.users"
        g0 = _draft([_single("users", src, "user_id", attrs=[_attr("user_id", src, "id")])], [])
        g1 = _draft([_single("users", src, "id", attrs=[_attr("id", src, "id")])], [])

        merged = merge_group_drafts([g0, g1], "dynamodb", notes=notes)

        assert merged["trade_offs"] == []
        assert merged["table_definitions"][0]["partition_key"]["attribute_name"] == "user_id"
        assert merge_failures(merged) == []

    def test_no_duplicate_gsis_in_the_merged_design(self):
        src = "discourse.users"
        attrs = [_attr("user_id", src), _attr("username_lower", src, type_="S")]

        def users(name: str, projection: str) -> dict:
            gsi = _gsi(name, ["username_lower"], projection=projection)
            gsi["partition_key"] = [_key("username_lower", "S")]
            return _single("users", src, "user_id", attrs=attrs, gsis=[gsi])

        g0 = _draft(
            [users("UsersByUsernameLower", "KEYS_ONLY")],
            [
                _ap(
                    "DDB-AP-1",
                    "users",
                    "Query",
                    "PK=username_lower",
                    gsi="UsersByUsernameLower",
                    source=src,
                )
            ],
        )
        g1 = _draft(
            [users("UsersByUsername", "ALL")],
            [
                _ap(
                    "DDB-AP-1",
                    "users",
                    "Query",
                    "PK=username_lower",
                    gsi="UsersByUsername",
                    source=src,
                )
            ],
        )
        g1["hot_partition_analysis"] = [
            {"table_name": "users", "gsi_name": "UsersByUsername", "operation": "read"}
        ]

        merged = merge_group_drafts([g0, g1], "dynamodb")

        [table] = merged["table_definitions"]
        [gsi] = table["gsis"]
        assert gsi["gsi_name"] in ("UsersByUsernameLower", "UsersByUsername")
        assert gsi["projection"] == "ALL"  # the wider projection is kept
        assert {ap["gsi_name"] for ap in merged["access_patterns"]} == {gsi["gsi_name"]}
        assert [h["gsi_name"] for h in merged["hot_partition_analysis"]] == [gsi["gsi_name"]]

    def test_hot_partitions_move_only_for_patterns_that_moved(self):
        drafts = TestDifferentKeySchemas()._two_groups()
        drafts[0]["access_patterns"].append(
            _ap("DDB-AP-2", "Orders", "Query", "PK=customer_id AND SK=:d")
        )
        drafts[0]["access_patterns"][1]["operation"] = "UpdateItem"
        drafts[0]["hot_partition_analysis"] = [
            {
                "table_name": "Orders",
                "gsi_name": None,
                "operation": "read",
                "contributing_patterns": ["q-DDB-AP-1"],
            },
            {
                "table_name": "Orders",
                "gsi_name": None,
                "operation": "write",
                "contributing_patterns": ["q-DDB-AP-2"],
            },
        ]

        merged = merge_group_drafts(drafts, "dynamodb", unique_keys=UNIQUE)

        [orders] = merged["table_definitions"]
        [gsi] = [g["gsi_name"] for g in orders["gsis"]]
        by_op = {
            e["operation"]: e
            for e in merged["hot_partition_analysis"]
            if e["table_name"] == "Orders"
        }
        assert by_op["read"]["gsi_name"] == gsi
        assert by_op["write"]["gsi_name"] is None


class TestMergeSchemaGroupsUsesTheSourceSchema:
    def test_collector_unique_keys_and_notes_file(self, tmp_path):
        from src.agents.schema_design.group_merger import merge_schema_groups
        from src.storage.local_store import LocalArtifactStore

        store = LocalArtifactStore(str(tmp_path))
        base = "shop/job-1/schema-dynamodb/v1"
        drafts = TestDifferentKeySchemas()._two_groups()
        store.write_json(
            "shop/job-1/collector/output.json",
            {
                "database_schema": {
                    "tables": [
                        {"table_id": ORDERS, "table_name": "orders", "primary_key": ["order_id"]}
                    ]
                }
            },
        )
        store.write_json(
            f"{base}/groups_manifest.json",
            {"groups": [{"group_index": 0}, {"group_index": 1}]},
        )
        for i, d in enumerate(drafts):
            store.write_json(f"{base}/schema_draft_group_{i}.json", d)

        merged = merge_schema_groups("job-1", "shop", "dynamodb", store)

        assert _keys(merged["table_definitions"][0]) == ("order_id", None)
        assert merge_failures(merged) == []


# ---------------------------------------------------------------------------
# Same keys under different attribute names
# ---------------------------------------------------------------------------


class TestKeyAttributeNames:
    def test_same_column_under_two_names_is_one_key(self):
        """Group 0 calls users.id ``user_id``, group 1 calls it ``id``."""
        src = "discourse.users"
        g0 = _draft(
            [
                _single(
                    "users",
                    src,
                    "user_id",
                    attrs=[_attr("user_id", src, "id"), _attr("username", src)],
                )
            ],
            [_ap("DDB-AP-1", "users", "GetItem", "PK=user_id", source=src)],
        )
        g1 = _draft(
            [
                _single(
                    "users",
                    src,
                    "id",
                    attrs=[_attr("id", src, "id"), _attr("admin", src)],
                    gsis=[_gsi("AdminUsers", ["admin"], ["id"])],
                )
            ],
            [
                _ap("DDB-AP-1", "users", "GetItem", "PK=id", source=src),
                _ap("DDB-AP-2", "users", "Query", "PK=admin", gsi="AdminUsers", source=src),
            ],
        )

        merged = merge_group_drafts([g0, g1], "dynamodb")

        [users] = merged["table_definitions"]
        assert _keys(users) == ("user_id", None)
        assert _gsi_keys(users) == {"AdminUsers": (("admin",), ("user_id",))}
        assert sorted(a["name"] for a in users["attributes"]) == ["admin", "user_id", "username"]
        assert merge_failures(merged) == [] and merge_warnings(merged) == []
        moved = next(ap for ap in merged["access_patterns"] if ap["pattern_id"] == "DDB-AP-2")
        assert moved["table_name"] == "users" and moved["gsi_name"] is None
        assert "user_id" in moved["key_condition"]
        _assert_every_pattern_is_served(merged)

    def test_different_table_names_for_the_same_rows_become_one_table(self):
        src = "wordpress.wp_actionscheduler_groups"
        g0 = _draft(
            [_single("WpActionSchedulerGroups", src, "group_id", attrs=[_attr("group_id", src)])],
            [_ap("DDB-AP-1", "WpActionSchedulerGroups", "GetItem", "PK=group_id", source=src)],
        )
        g1 = _draft(
            [_single("wp_actionscheduler_groups", src, "id", attrs=[_attr("id", src, "group_id")])],
            [_ap("DDB-AP-1", "wp_actionscheduler_groups", "GetItem", "PK=id", source=src)],
        )

        merged = merge_group_drafts([g0, g1], "dynamodb")

        # One group each, one pattern each: an exact tie, alphabetical ignoring case.
        assert [t["table_name"] for t in merged["table_definitions"]] == [
            "wp_actionscheduler_groups"
        ]
        assert merge_warnings(merged) == []
        _assert_every_pattern_is_served(merged)


# ---------------------------------------------------------------------------
# GSIs with the same name and different keys
# ---------------------------------------------------------------------------


class TestGsiNames:
    def test_same_gsi_name_with_different_keys_keeps_both(self):
        """The WpOptions / OptionsByAutoload shape."""
        src = "wordpress.wp_options"
        attrs = [_attr("option_name", src, type_="S"), _attr("autoload", src, type_="S")]

        def options(gsi: dict) -> dict:
            t = _single("WpOptions", src, "option_name", attrs=attrs, gsis=[gsi])
            t["partition_key"] = _key("option_name", "S")
            return t

        g0 = _draft(
            [options(_gsi("OptionsByAutoload", ["autoload"]))],
            [
                _ap(
                    "DDB-AP-1",
                    "WpOptions",
                    "Query",
                    "PK=autoload",
                    gsi="OptionsByAutoload",
                    source=src,
                )
            ],
        )
        g1 = _draft(
            [options(_gsi("OptionsByAutoload", ["autoload"], ["option_name"]))],
            [
                _ap(
                    "DDB-AP-1",
                    "WpOptions",
                    "Query",
                    "PK=autoload AND SK begins_with :p",
                    gsi="OptionsByAutoload",
                    source=src,
                )
            ],
        )

        merged = merge_group_drafts([g0, g1], "dynamodb")

        [table] = merged["table_definitions"]
        keys = _gsi_keys(table)
        assert keys["OptionsByAutoload"] == (("autoload",), ())
        assert (("autoload",), ("option_name",)) in keys.values()
        assert len(keys) == 2
        assert merge_failures(merged) == []
        [second] = [ap for ap in merged["access_patterns"] if "begins_with" in ap["key_condition"]]
        assert keys[second["gsi_name"]] == (("autoload",), ("option_name",))
        _assert_every_pattern_is_served(merged)


# ---------------------------------------------------------------------------
# Item collections
# ---------------------------------------------------------------------------

POSTS = "wordpress.wp_posts"
POSTMETA = "wordpress.wp_postmeta"


class TestItemCollections:
    def test_sort_key_name_and_entity_labels_are_aligned(self):
        """Group 0: SK ``sk``, POST_META; group 1: SK ``entity_key``, POSTMETA."""
        post = _entity("POST", POSTS, "{ID}", "POST", _attr("post_id", POSTS, "ID"))
        g0 = _draft(
            [
                _collection(
                    "WpPosts",
                    "post_id",
                    "sk",
                    post,
                    _entity("POST_META", POSTMETA, "{post_id}", "META#{meta_key}"),
                )
            ],
            [
                _ap(
                    "DDB-AP-1",
                    "WpPosts",
                    "Query",
                    "PK=post_id AND sk begins_with 'META#'",
                    source=POSTMETA,
                )
            ],
        )
        g1 = _draft(
            [
                _collection(
                    "WpPosts",
                    "post_id",
                    "entity_key",
                    post,
                    _entity("POSTMETA", POSTMETA, "{post_id}", "META#{meta_key}"),
                    _entity(
                        "TERM_REL",
                        "wordpress.wp_term_relationships",
                        "{object_id}",
                        "TERM#{term_taxonomy_id}",
                    ),
                )
            ],
            [
                _ap(
                    "DDB-AP-1",
                    "WpPosts",
                    "Query",
                    "PK=post_id AND entity_key begins_with 'TERM#'",
                    source=POSTS,
                )
            ],
        )

        merged = merge_group_drafts([g0, g1], "dynamodb")

        [table] = merged["table_definitions"]
        # One name each: the most tables, then the longer name (never the draft order).
        assert _keys(table) == ("post_id", "entity_key")
        assert sorted(e["entity_type"] for e in table["entities"]) == [
            "POST",
            "POST_META",
            "TERM_REL",
        ]
        assert merge_failures(merged) == []
        _assert_every_pattern_is_served(merged)

    def test_same_name_collections_without_a_shared_entity_merge(self):
        meta = _entity("TERM_META", "wordpress.wp_termmeta", "{term_id}", "META#{meta_key}")
        term = _entity("TERM", "wordpress.wp_terms", "{term_id}", "TERM")
        g0 = _draft([_collection("WpTerms", "term_id", "sk", meta)], [])
        g1 = _draft([_collection("WpTerms", "term_id", "sk", term)], [])

        merged = merge_group_drafts([g0, g1], "dynamodb")

        [table] = merged["table_definitions"]
        assert [e["entity_type"] for e in table["entities"]] == ["TERM_META", "TERM"]
        assert merge_failures(merged) == []

    def test_single_table_folds_into_the_collection_entity_for_the_same_rows(self):
        """wp_termmeta (PK term_id, SK meta_id) vs entity TERM_META in WpTerms."""
        src = "wordpress.wp_termmeta"
        meta = _entity(
            "TERM_META",
            src,
            "{term_id}",
            "META#{meta_id}",
            _attr("term_id", src),
            _attr("meta_id", src),
        )
        term = _entity("TERM", "wordpress.wp_terms", "{term_id}", "TERM")
        g0 = _draft(
            [_collection("WpTerms", "term_id", "sk", term, meta)],
            [
                _ap(
                    "DDB-AP-1",
                    "WpTerms",
                    "GetItem",
                    "PK=term_id AND sk='TERM'",
                    source="wordpress.wp_terms",
                )
            ],
        )
        g1 = _draft(
            [
                _single(
                    "wp_termmeta",
                    src,
                    "term_id",
                    "meta_id",
                    attrs=[
                        _attr("term_id", src),
                        _attr("meta_id", src),
                        _attr("meta_value", src, type_="S"),
                    ],
                )
            ],
            [
                _ap("DDB-AP-1", "wp_termmeta", "Query", "PK=term_id", source=src),
                _ap("DDB-AP-2", "wp_termmeta", "GetItem", "PK=term_id AND SK=meta_id", source=src),
            ],
        )

        merged = merge_group_drafts([g0, g1], "dynamodb")

        [table] = merged["table_definitions"]
        assert table["table_name"] == "WpTerms"
        [entity] = [e for e in table["entities"] if e["entity_type"] == "TERM_META"]
        assert "meta_value" in [a["name"] for a in entity["attributes"]]
        assert merge_failures(merged) == [] and merge_warnings(merged) == []
        query = next(ap for ap in merged["access_patterns"] if ap["pattern_id"] == "DDB-AP-2")
        assert query["table_name"] == "WpTerms"
        assert "begins_with 'META#'" in query["key_condition"]
        _assert_every_pattern_is_served(merged)


USERS = "discourse.users"


class TestMoreItemCollectionShapes:
    def test_static_sort_keys_for_the_same_rows_take_the_first_groups_value(self):
        """Group 0 stores a user under SK ``PROFILE``, group 1 under ``USER``."""
        user = _attr("user_id", USERS, "id")
        g0 = _draft(
            [
                _collection(
                    "discourse_users",
                    "user_id",
                    "sk",
                    _entity("USER", USERS, "{id}", "PROFILE", user),
                )
            ],
            [
                _ap(
                    "DDB-AP-1",
                    "discourse_users",
                    "GetItem",
                    "PK=user_id AND sk='PROFILE'",
                    source=USERS,
                )
            ],
        )
        g1 = _draft(
            [
                _collection(
                    "discourse_users",
                    "user_id",
                    "sk",
                    _entity("USER", USERS, "{user_id}", "USER", user),
                )
            ],
            [
                _ap(
                    "DDB-AP-1",
                    "discourse_users",
                    "GetItem",
                    "PK=user_id AND sk='USER'",
                    source=USERS,
                )
            ],
        )

        merged = merge_group_drafts([g0, g1], "dynamodb")

        [table] = merged["table_definitions"]
        [entity] = table["entities"]
        assert (entity["pk_template"], entity["sk_template"]) == ("{id}", "PROFILE")
        assert merge_failures(merged) == []
        moved = merged["access_patterns"][1]
        assert "sort key 'USER' is 'PROFILE'" in moved["key_condition"]

    def test_single_table_becomes_a_new_entity_of_the_collection_with_its_name(self):
        """Group 0: ``users`` holds user rows; group 1: ``users`` holds only their emails."""
        emails = "discourse.user_emails"
        g0 = _draft(
            [_single("users", USERS, "user_id", attrs=[_attr("user_id", USERS, "id")])],
            [_ap("DDB-AP-1", "users", "GetItem", "PK=user_id", source=USERS)],
        )
        g1 = _draft(
            [
                _collection(
                    "users",
                    "user_id",
                    "sk",
                    _entity(
                        "USER_EMAIL", emails, "{user_id}", "EMAIL#{id}", _attr("user_id", emails)
                    ),
                )
            ],
            [
                _ap(
                    "DDB-AP-1",
                    "users",
                    "Query",
                    "PK=user_id AND sk begins_with 'EMAIL#'",
                    source=emails,
                )
            ],
        )

        merged = merge_group_drafts([g0, g1], "dynamodb")

        [table] = merged["table_definitions"]
        assert table["aggregate_pattern"] == "item_collection"
        assert {e["entity_type"]: e["sk_template"] for e in table["entities"]} == {
            "USERS": "USERS",
            "USER_EMAIL": "EMAIL#{id}",
        }
        assert merge_failures(merged) == []
        get = _pattern(merged, "GetItem")
        assert "sk='USERS'" in get["key_condition"]
        _assert_every_pattern_is_served(merged)

    def test_differently_named_collections_holding_the_same_rows_merge(self):
        meta = "wordpress.wp_termmeta"
        term = _entity("TERM", "wordpress.wp_terms", "{term_id}", "TERM")
        g0 = _draft(
            [
                _collection(
                    "WpTerms",
                    "term_id",
                    "sk",
                    term,
                    _entity("TERM_META", meta, "{term_id}", "META#{meta_id}"),
                )
            ],
            [_ap("DDB-AP-1", "WpTerms", "Query", "PK=term_id", source=meta)],
        )
        g1 = _draft(
            [
                _collection(
                    "WpTermMeta",
                    "term_id",
                    "entity_sk",
                    _entity("TERMMETA", meta, "{term_id}", "META#{meta_id}"),
                )
            ],
            [
                _ap(
                    "DDB-AP-1",
                    "WpTermMeta",
                    "Query",
                    "PK=term_id AND entity_sk begins_with 'META#'",
                    source=meta,
                )
            ],
        )

        merged = merge_group_drafts([g0, g1], "dynamodb")

        # One group each, one pattern each: an exact tie, alphabetical ignoring case.
        assert [t["table_name"] for t in merged["table_definitions"]] == ["WpTermMeta"]
        assert merge_failures(merged) == [] and merge_warnings(merged) == []
        _assert_every_pattern_is_served(merged)

    def test_template_tokens_are_compared_by_source_column(self):
        """``{ID}`` and ``{post_id}`` (an attribute copying ID) are the same key."""
        by_column = _entity("POST", POSTS, "{ID}", "POST", _attr("post_id", POSTS, "ID"))
        by_attribute = _entity("POST", POSTS, "{post_id}", "POST", _attr("post_id", POSTS, "ID"))
        g0 = _draft([_collection("WpPosts", "post_id", "sk", by_column)], [])
        g1 = _draft([_collection("WpPostItems", "post_id", "sk", by_attribute)], [])

        merged = merge_group_drafts([g0, g1], "dynamodb")

        assert merge_failures(merged) == []

    def test_sort_keys_over_different_columns_stay_a_conflict(self):
        meta = "wordpress.wp_termmeta"
        g0 = _draft(
            [
                _collection(
                    "WpTerms",
                    "term_id",
                    "sk",
                    _entity("TERM_META", meta, "{term_id}", "META#{meta_key}#{meta_id}"),
                )
            ],
            [],
        )
        g1 = _draft(
            [
                _collection(
                    "WpTerms",
                    "term_id",
                    "sk",
                    _entity("TERM_META", meta, "{term_id}", "META#{meta_id}"),
                )
            ],
            [],
        )

        merged = merge_group_drafts([g0, g1], "dynamodb")

        assert len(merge_failures(merged)) == 1


class TestInputs:
    def test_drafts_passed_in_are_not_modified(self):
        import copy

        drafts = TestDifferentKeySchemas()._two_groups()
        before = copy.deepcopy(drafts)

        merge_group_drafts(drafts, "dynamodb")

        assert drafts == before


# ---------------------------------------------------------------------------
# What stays a conflict (or untouched)
# ---------------------------------------------------------------------------


class TestLimits:
    def test_same_name_for_different_rows_still_fails(self):
        g0 = _draft([_single("Main", "shop.users", "id")], [])
        g1 = _draft([_single("Main", "shop.orders", "order_id")], [])

        merged = merge_group_drafts([g0, g1], "dynamodb")

        assert len(merge_failures(merged)) == 1
        assert merged["validation_passed"] is False

    def test_two_tables_one_group_designed_on_purpose_are_left_alone(self):
        g0 = _draft(
            [
                _single("OrdersById", ORDERS, "order_id"),
                _single("OrdersByCustomer", ORDERS, "customer_id"),
            ],
            [],
        )
        g1 = _draft([_single("Orders", ORDERS, "order_date")], [])

        merged = merge_group_drafts([g0, g1], "dynamodb")

        assert {_keys(t) for t in merged["table_definitions"]} == {
            ("order_id", None),
            ("customer_id", None),
            ("order_date", None),
        }

    def test_trade_off_keeping_separate_tables_is_respected(self):
        keep = {
            "description": "orders are kept twice on purpose",
            "impact": "streams keep them in sync",
            "source_tables": [ORDERS],
            "target_tables": ["OrdersById", "OrdersByCustomer"],
            "query_ids": [],
            "engine": "dynamodb",
        }
        g0 = _draft([_single("OrdersById", ORDERS, "order_id")], [], [keep])
        g1 = _draft([_single("OrdersByCustomer", ORDERS, "customer_id")], [])

        merged = merge_group_drafts([g0, g1], "dynamodb")

        assert len(merged["table_definitions"]) == 2

    def test_more_gsis_than_dynamodb_allows_is_a_failure(self):
        many = [_gsi(f"G{i}", [f"a{i}"]) for i in range(GSI_LIMIT)]
        attrs = [_attr("order_id", ORDERS), _attr("customer_id", ORDERS)] + [
            _attr(f"a{i}", ORDERS) for i in range(GSI_LIMIT)
        ]
        g0 = _draft(
            [_single("Orders", ORDERS, "order_id", attrs=attrs, gsis=many)],
            [_ap("DDB-AP-1", "Orders", "GetItem", "PK=order_id")],
        )
        g1 = _draft(
            [_single("Orders", ORDERS, "customer_id", attrs=attrs)],
            [_ap("DDB-AP-1", "Orders", "Query", "PK=customer_id")],
        )

        merged = merge_group_drafts([g0, g1], "dynamodb", unique_keys=UNIQUE)

        [failure] = merge_failures(merged)
        assert "Orders" in failure and str(GSI_LIMIT) in failure


# ---------------------------------------------------------------------------
# Round 3 of the #803 review: nullable unique indexes, determinism, the trace
# ---------------------------------------------------------------------------

CHANNELS = "discourse.chat_channels"


def _chat_channels_collector(slug_nullable: bool) -> dict:
    return {
        "database_schema": {
            "tables": [
                {
                    "table_id": CHANNELS,
                    "table_name": "chat_channels",
                    "primary_key": ["id"],
                    "columns": [
                        {"column_name": "id", "nullable": False},
                        {"column_name": "slug", "nullable": slug_nullable},
                    ],
                    "indexes": [
                        {
                            "index_name": "pk",
                            "columns": ["id"],
                            "is_unique": True,
                            "is_primary": True,
                        },
                        {"index_name": "by_slug", "columns": ["slug"], "is_unique": True},
                    ],
                }
            ]
        }
    }


def _chat_channels_drafts() -> list[dict]:
    attrs = [_attr("id", CHANNELS), _attr("slug", CHANNELS, type_="S")]
    by_slug = _single("chat_channels", CHANNELS, "slug", attrs=attrs)
    by_slug["partition_key"] = _key("slug", "S")
    g0 = _draft(
        [by_slug],
        [
            _ap(f"DDB-AP-{i}", "chat_channels", "GetItem", "PK=slug", source=CHANNELS)
            for i in (1, 2, 3)
        ],
    )
    g1 = _draft(
        [_single("chat_channels", CHANNELS, "id", attrs=attrs)],
        [_ap("DDB-AP-1", "chat_channels", "GetItem", "PK=id", source=CHANNELS)],
    )
    return [g0, g1]


class TestNullableUniqueIndexes:
    def test_a_unique_index_on_a_nullable_column_is_no_proof_of_a_key(self):
        """SQL allows many NULL slugs; DynamoDB cannot store an item with no key value."""
        from src.agents.schema_design.dynamodb_key_reconcile import unique_keys_from_collector

        unique = unique_keys_from_collector(_chat_channels_collector(slug_nullable=True))
        assert unique == {"chat_channels": [frozenset({"id"})]}

        merged = merge_group_drafts(_chat_channels_drafts(), "dynamodb", unique_keys=unique)

        [table] = merged["table_definitions"]
        assert _keys(table) == ("id", None)
        assert (("slug",), ()) in _gsi_keys(table).values()
        _assert_every_pattern_is_served(merged)

    def test_a_unique_index_on_not_null_columns_still_counts(self):
        from src.agents.schema_design.dynamodb_key_reconcile import unique_keys_from_collector

        unique = unique_keys_from_collector(_chat_channels_collector(slug_nullable=False))

        assert unique == {"chat_channels": [frozenset({"id"}), frozenset({"slug"})]}


class TestDeterminism:
    def test_merging_the_same_drafts_twice_gives_identical_output(self):
        import json

        def run() -> str:
            notes: list[str] = []
            drafts = (
                TestDifferentKeySchemas()._two_groups()
                + TestNoRowCollapse()._topic_tags()
                + _chat_channels_drafts()
            )
            merged = merge_group_drafts(drafts, "dynamodb", unique_keys=UNIQUE, notes=notes)
            return json.dumps({"merged": merged, "notes": notes}, sort_keys=True)

        assert run() == run()


def _every_order(drafts: list[dict]):
    """``merge_group_drafts`` over every order of ``drafts``, each keeping its group index."""
    import copy
    import itertools

    for order in itertools.permutations(range(len(drafts))):
        yield (
            order,
            merge_group_drafts(
                [copy.deepcopy(drafts[i]) for i in order], "dynamodb", group_indices=list(order)
            ),
        )


class TestDraftOrder:
    """Draft order and group numbering never decide the merged design (#809)."""

    def _custom_field_drafts(self) -> list[dict]:
        """Groups 0, 1 and 3 agree on a custom-field sort key; group 2 does not."""
        fields = "discourse.user_custom_fields"

        def users(sk: str) -> dict:
            return _collection(
                "discourse_users",
                "user_id",
                "sk",
                _entity("USER", USERS, "{id}", "PROFILE", _attr("id", USERS)),
                _entity(
                    "USER_CUSTOM_FIELD",
                    fields,
                    "{user_id}",
                    sk,
                    _attr("user_id", fields),
                    _attr("id", fields),
                    _attr("name", fields, type_="S"),
                ),
            )

        return [
            _draft(
                [users(sk)],
                [_ap(f"DDB-AP-{g}", "discourse_users", "Query", "PK=user_id", source=fields)],
            )
            for g, sk in enumerate(
                ["CUSTOM_FIELD#{id}"] * 2 + ["CUSTOM_FIELD#{name}", "CUSTOM_FIELD#{id}"]
            )
        ]

    def test_the_design_most_groups_agree_on_is_kept(self):
        for order, merged in _every_order(self._custom_field_drafts()):
            [failure] = merge_failures(merged)
            assert "group 0/1/3 and group 2" in failure, order
            [table] = merged["table_definitions"]
            sks = {e["entity_type"]: e["sk_template"] for e in table["entities"]}
            assert sks["USER_CUSTOM_FIELD"] == "CUSTOM_FIELD#{id}", order

    def test_entity_labels_are_aligned_across_every_collection_for_the_same_rows(self):
        """Group 0 labels links, tags and solved topics one way, group 1 another, group 2 has
        only two of them: whatever the order, the three collections become one table."""
        links, tags, solved = "discourse.topic_links", "discourse.topic_tags", "discourse.solved"

        def items(name: str, sk: str, link: str | None, tag: str, done: str) -> dict:
            entities = [_entity(done, solved, "{topic_id}", "SOLVED", _attr("topic_id", solved))]
            if link:
                entities.append(
                    _entity(link, links, "{topic_id}", "LINK#{id}", _attr("topic_id", links))
                )
            entities.append(
                _entity(
                    tag,
                    tags,
                    "{topic_id}",
                    "TAG#{tag_id}",
                    _attr("topic_id", tags),
                    _attr("tag_id", tags),
                )
            )
            return _collection(name, "topic_id", sk, *entities)

        drafts = [
            _draft(
                [items("discourse_topics", "sk", "TOPIC_LINK", "TOPIC_TAG", "SOLVED_TOPIC")], []
            ),
            _draft([items("discourse_topic_items", "sk", "LINK", "TAG", "SOLVED")], []),
            _draft([items("discourse_topic_items", "record_key", None, "TOPIC_TAG", "SOLVED")], []),
        ]

        for order, merged in _every_order(drafts):
            assert merge_failures(merged) == [], order
            [table] = merged["table_definitions"]
            assert table["table_name"] == "discourse_topic_items", order
            assert _keys(table) == ("topic_id", "sk"), order
            assert sorted(e["entity_type"] for e in table["entities"]) == [
                "SOLVED",
                "TOPIC_LINK",
                "TOPIC_TAG",
            ], order

    def test_a_gsi_name_stays_with_the_keys_most_groups_give_it(self):
        src = "wordpress.wp_options"
        attrs = [_attr(c, src, type_="S") for c in ("option_name", "autoload", "option_id")]

        def options(sort_key: list[str] | None) -> dict:
            gsi = _gsi("OptionsByAutoload", ["autoload"], sort_key)
            t = _single("WpOptions", src, "option_name", attrs=attrs, gsis=[gsi])
            t["partition_key"] = _key("option_name", "S")
            return t

        drafts = [
            _draft([options(["option_id"])], []),
            _draft([options(None)], []),
            _draft([options(["option_id"])], []),
        ]

        for order, merged in _every_order(drafts):
            [table] = merged["table_definitions"]
            assert _gsi_keys(table) == {
                "OptionsByAutoload": (("autoload",), ("option_id",)),
                "OptionsByAutoload_Autoload": (("autoload",), ()),
            }, order

    def test_two_items_per_row_are_never_relabelled_onto_each_other(self):
        """Group 0 stores a user as two items (PROFILE and STATS); group 1 has only PROFILE,
        group 2 only STATS. Group 1's read of SK=PROFILE must never be pointed at STATS:
        the labels are ambiguous, so the disagreement stays a reported conflict."""

        def users(*entities: tuple[str, str]) -> dict:
            return _collection(
                "users",
                "user_id",
                "sk",
                *(_entity(t, USERS, "{id}", sk, _attr("id", USERS)) for t, sk in entities),
            )

        drafts = [
            _draft([users(("USER_PROFILE", "PROFILE"), ("USER_STATS", "STATS"))], []),
            _draft(
                [users(("USER", "PROFILE"))],
                [
                    _ap(
                        "DDB-AP-1",
                        "users",
                        "GetItem",
                        "PK=user_id AND SK=PROFILE",
                        source=USERS,
                    )
                ],
            ),
            _draft([users(("USER_STATS", "STATS"))], []),
        ]

        for order, merged in _every_order(drafts):
            read = _pattern(merged, "GetItem")
            assert "STATS" not in read["key_condition"], order
            assert any("PROFILE" in f for f in merge_failures(merged)), order
            for table in merged["table_definitions"]:
                by_type = {e["entity_type"]: e["sk_template"] for e in table.get("entities") or []}
                assert by_type.get("USER", "PROFILE") == "PROFILE", order
                assert by_type.get("USER_PROFILE", "PROFILE") == "PROFILE", order

    def test_the_table_name_most_groups_use_survives(self):
        """Five groups say posts, three say discourse_posts: posts, in every order."""
        src = "discourse.posts"

        def posts(name: str) -> dict:
            return _single(name, src, "post_id", attrs=[_attr("post_id", src, "id")])

        names = ["posts"] * 3 + ["discourse_posts"] * 2
        drafts = [
            _draft([posts(n)], [_ap("DDB-AP-1", n, "GetItem", "PK=post_id", source=src)])
            for n in names
        ]

        for order, merged in _every_order(drafts):
            assert [t["table_name"] for t in merged["table_definitions"]] == ["posts"], order
            assert {ap["table_name"] for ap in merged["access_patterns"]} == {"posts"}, order

    def test_table_names_are_voted_case_insensitively(self):
        """badges (2 groups) beats Badges (1); api_keys (2) beats ApiKeys (1)."""
        badges, keys = "discourse.badges", "discourse.api_keys"

        def draft(badge_name: str, key_name: str) -> dict:
            return _draft(
                [
                    _single(
                        badge_name, badges, "badge_id", attrs=[_attr("badge_id", badges, "id")]
                    ),
                    _single(key_name, keys, "api_key_id", attrs=[_attr("api_key_id", keys, "id")]),
                ],
                [
                    _ap("DDB-AP-1", badge_name, "GetItem", "PK=badge_id", source=badges),
                    _ap("DDB-AP-2", key_name, "GetItem", "PK=api_key_id", source=keys),
                ],
            )

        drafts = [
            draft("Badges", "ApiKeys"),
            draft("badges", "api_keys"),
            draft("badges", "api_keys"),
        ]

        for order, merged in _every_order(drafts):
            assert sorted(t["table_name"] for t in merged["table_definitions"]) == [
                "api_keys",
                "badges",
            ], order

    def test_exact_name_ties_are_alphabetical_ignoring_case(self):
        src = "discourse.badges"
        drafts = [
            _draft([_single(n, src, "badge_id", attrs=[_attr("badge_id", src, "id")])], [])
            for n in ("Discourse_badges", "badges")
        ]

        for order, merged in _every_order(drafts):
            assert [t["table_name"] for t in merged["table_definitions"]] == ["badges"], order

    def test_fold_notes_name_the_final_table(self):
        """A table folded into a collection that the merge then renames: the note says where
        the pattern really runs."""
        src = "discourse.user_stats"
        stats = _single("user_stats", src, "user_id", attrs=[_attr("user_id", src)])
        get = _ap("DDB-AP-1", "user_stats", "GetItem", "PK=user_id", source=src)

        def items(name: str, *more: dict) -> dict:
            user = _entity("USER", USERS, "{id}", "PROFILE", _attr("id", USERS))
            return _collection(name, "user_id", "sk", user, *more)

        drafts = [
            _draft([stats], [get]),
            _draft(
                [
                    items(
                        "user_items",
                        _entity("USER_STATS", src, "{user_id}", "STATS", _attr("user_id", src)),
                    )
                ],
                [],
            ),
            *(_draft([items("users")], []) for _ in range(3)),
        ]

        for order, merged in _every_order(drafts):
            [table] = merged["table_definitions"]
            assert table["table_name"] == "users", order
            read = _pattern(merged, "GetItem")
            assert read["table_name"] == "users", order
            assert "in users as USER_STATS" in read["key_condition"], (order, read)

    def test_pattern_ids_do_not_follow_draft_order(self):
        """Every group numbers from DDB-AP-1: each query keeps one merged ID in every order."""
        drafts = self._custom_field_drafts()
        for d in drafts:
            d["access_patterns"][0]["pattern_id"] = "DDB-AP-1"

        numbering = {
            json.dumps(
                sorted((ap["query_ids"][0], ap["pattern_id"]) for ap in merged["access_patterns"])
            )
            for _, merged in _every_order(drafts)
        }

        assert len(numbering) == 1


class TestMergeTrace:
    def _store(self, tmp_path, collector: dict | None):
        from src.storage.local_store import LocalArtifactStore

        store = LocalArtifactStore(str(tmp_path))
        base = "shop/job-1/schema-dynamodb/v1"
        src = "discourse.users"
        drafts = [
            _draft([_single("users", src, "user_id", attrs=[_attr("user_id", src, "id")])], []),
            _draft([_single("users", src, "id", attrs=[_attr("id", src, "id")])], []),
        ]
        if collector is not None:
            store.write_json("shop/job-1/collector/output.json", collector)
        store.write_json(
            f"{base}/groups_manifest.json", {"groups": [{"group_index": 0}, {"group_index": 1}]}
        )
        for i, d in enumerate(drafts):
            store.write_json(f"{base}/schema_draft_group_{i}.json", d)
            store.write_json(f"{base}/design_trace_group_{i}.json", {"group": i})
        return store, base

    def test_reconciliation_notes_go_into_the_design_trace(self, tmp_path):
        from src.agents.schema_design.group_merger import merge_schema_groups

        store, base = self._store(tmp_path, {"database_schema": {"tables": []}})

        merge_schema_groups("job-1", "shop", "dynamodb", store)

        trace = store.read_json(f"{base}/design_trace.json")
        assert trace["total_groups"] == 2
        assert any("users" in n for n in trace["merge_reconciliation"])
        assert not store.exists(f"{base}/merge_reconciliation.json")

    def test_a_later_merge_without_notes_leaves_no_stale_notes(self, tmp_path):
        from src.agents.schema_design.group_merger import merge_schema_groups

        store, base = self._store(tmp_path, {"database_schema": {"tables": []}})
        merge_schema_groups("job-1", "shop", "dynamodb", store)
        same = _draft([_single("users", "discourse.users", "user_id")], [])
        for i in (0, 1):
            store.write_json(f"{base}/schema_draft_group_{i}.json", same)

        merge_schema_groups("job-1", "shop", "dynamodb", store)

        assert store.read_json(f"{base}/design_trace.json")["merge_reconciliation"] == []

    def test_missing_collector_output_is_logged(self, tmp_path, caplog):
        import logging

        from src.agents.schema_design.group_merger import merge_schema_groups

        store, _ = self._store(tmp_path, None)

        with caplog.at_level(logging.WARNING):
            merge_schema_groups("job-1", "shop", "dynamodb", store)

        assert "collector output" in caplog.text and "unique keys" in caplog.text


class TestFoldKeepsKeyTypes:
    def test_a_single_table_never_folds_into_a_collection_with_another_pk_type(self):
        """PR #803 discourse run: users (PK user_id N) folded into a collection keyed by S."""
        src = "discourse.users"
        user = _entity("USER", src, "{id}", "PROFILE", _attr("user_id", src, "id"))
        string_keyed = _collection("discourse_users", "user_id", "sk", user)
        string_keyed["partition_key"] = _key("user_id", "S")
        g0 = _draft([string_keyed], [])
        g1 = _draft(
            [_single("users", src, "user_id", attrs=[_attr("user_id", src, "id")])],
            [_ap("DDB-AP-1", "users", "GetItem", "PK=user_id", source=src)],
        )

        merged = merge_group_drafts([g0, g1], "dynamodb")

        tables = _by_name(merged)
        assert tables["users"]["partition_key"]["attribute_type"] == "N"
        assert merged["access_patterns"][0]["table_name"] == "users"


# ---------------------------------------------------------------------------
# Partition-key type disagreements resolved from the source column (#803 round 5)
# ---------------------------------------------------------------------------

TOPICS = "discourse.topics"


def _collector_columns(*tables: tuple[str, list[tuple[str, str | None, str]]]) -> dict:
    """Collector output with (column, normalized type, native type) per table."""
    return {
        "database_schema": {
            "tables": [
                {
                    "table_id": table_id,
                    "table_name": table_id.split(".")[-1],
                    "columns": [
                        {
                            "column_name": c,
                            "normalized_data_type": n,
                            "data_type": d,
                            "nullable": False,
                        }
                        for c, n, d in columns
                    ],
                }
                for table_id, columns in tables
            ]
        }
    }


DISCOURSE_COLUMNS = _collector_columns(
    (TOPICS, [("id", None, "integer"), ("title", "string", "character varying")]),
    (TOPIC_TAGS, [("topic_id", None, "integer"), ("tag_id", None, "integer")]),
    ("discourse.users", [("id", None, "integer"), ("username", "string", "character varying")]),
)


def _topics_collection(pk_type: str, sk_type: str = "S") -> dict:
    topic = _entity(
        "TOPIC", TOPICS, "{id}", "TOPIC", _attr("topic_id", TOPICS, "id", type_=pk_type)
    )
    tag = _entity(
        "TAG",
        TOPIC_TAGS,
        "{topic_id}",
        "TAG#{tag_id}",
        _attr("topic_id", TOPIC_TAGS, type_=pk_type),
    )
    table = _collection("discourse_topics", "topic_id", "sk", topic, tag)
    table["partition_key"] = _key("topic_id", pk_type)
    table["sort_key"] = _key("sk", sk_type)
    return table


def _merge_with_columns(drafts: list[dict], collector: dict) -> dict:
    from src.agents.schema_design.dynamodb_key_reconcile import (
        column_types_from_collector,
        unique_keys_from_collector,
    )

    return merge_group_drafts(
        drafts,
        "dynamodb",
        unique_keys=unique_keys_from_collector(collector),
        column_types=column_types_from_collector(collector),
    )


class TestPartitionKeyTypeFromTheSource:
    def test_discourse_topic_id_takes_the_integer_columns_type(self):
        """PR #803 discourse run: group 3 typed topic_id as S, the others as N."""
        g0 = _draft(
            [_topics_collection("N")],
            [_ap("DDB-AP-1", "discourse_topics", "Query", "PK=topic_id", source=TOPICS)],
        )
        g1 = _draft(
            [_topics_collection("S")],
            [_ap("DDB-AP-1", "discourse_topics", "Query", "PK=topic_id", source=TOPICS)],
        )

        merged = _merge_with_columns([g1, g0], DISCOURSE_COLUMNS)

        [table] = merged["table_definitions"]
        assert table["partition_key"] == _key("topic_id", "N")
        assert merge_failures(merged) == []
        [note] = [t for t in merged["trade_offs"] if "topic_id" in t["description"]]
        assert "discourse_topics" in note["description"]
        assert "S" in note["description"] and "N" in note["description"]
        assert (
            "partition key type only affects hashing; matches the source column" in note["impact"]
        )
        attr_types = {
            a["type"] for e in table["entities"] for a in e["attributes"] if a["name"] == "topic_id"
        }
        assert attr_types == {"N"}

    def test_discourse_user_id_on_single_entity_tables(self):
        src = "discourse.users"
        a = _single("users", src, "user_id", attrs=[_attr("user_id", src, "id")])
        b = _single("users", src, "user_id", attrs=[_attr("user_id", src, "id", type_="S")])
        b["partition_key"] = _key("user_id", "S")
        drafts = [
            _draft([b], [_ap("DDB-AP-1", "users", "GetItem", "PK=user_id", source=src)]),
            _draft([a], [_ap("DDB-AP-1", "users", "GetItem", "PK=user_id", source=src)]),
        ]

        merged = _merge_with_columns(drafts, DISCOURSE_COLUMNS)

        [table] = merged["table_definitions"]
        assert table["partition_key"] == _key("user_id", "N")
        assert merge_failures(merged) == []

    def test_text_column_becomes_s(self):
        src = "wordpress.wp_options"
        collector = _collector_columns((src, [("option_name", None, "varchar(191)")]))
        a = _single("WpOptions", src, "option_name", attrs=[_attr("option_name", src, type_="S")])
        a["partition_key"] = _key("option_name", "S")
        b = _single("WpOptions", src, "option_name", attrs=[_attr("option_name", src)])

        merged = _merge_with_columns([_draft([b], []), _draft([a], [])], collector)

        assert merged["table_definitions"][0]["partition_key"] == _key("option_name", "S")
        assert merge_failures(merged) == []

    def test_a_sort_key_type_disagreement_stays_a_failure(self):
        g0 = _draft([_topics_collection("N", sk_type="S")], [])
        g1 = _draft([_topics_collection("N", sk_type="N")], [])

        merged = _merge_with_columns([g0, g1], DISCOURSE_COLUMNS)

        [failure] = merge_failures(merged)
        assert "key attribute types differ (sk N vs S)" in failure or (
            "key attribute types differ (sk S vs N)" in failure
        )

    def test_an_unknown_source_type_stays_a_failure(self):
        unknown = _collector_columns((TOPICS, [("title", "string", "character varying")]))
        g0 = _draft([_topics_collection("N")], [])
        g1 = _draft([_topics_collection("S")], [])

        merged = _merge_with_columns([g0, g1], unknown)

        [failure] = merge_failures(merged)
        assert "key attribute types differ (topic_id N vs S)" in failure

    def test_a_partition_and_sort_key_type_disagreement_stays_a_failure(self):
        g0 = _draft([_topics_collection("N", sk_type="S")], [])
        g1 = _draft([_topics_collection("S", sk_type="N")], [])

        merged = _merge_with_columns([g0, g1], DISCOURSE_COLUMNS)

        assert len(merge_failures(merged)) == 1
        assert merged["table_definitions"][0]["partition_key"] == _key("topic_id", "N")
