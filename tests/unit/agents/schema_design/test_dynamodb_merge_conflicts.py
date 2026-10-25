"""A group's trade-off never keeps two DynamoDB homes for one source table (#333).

DynamoDB schema design always runs in groups. When two groups each give a source
table its own home (``TermMeta`` in one, a ``TERM_META`` entity inside ``Taxonomy``
in another), the merge reconciles them deterministically. Before #333 a group's own
trade-off naming both homes ("a second, independent home ... a migration must pick
one write path") was read as a justification: the merge skipped the reconciliation
and suppressed its review note, so the merged design shipped two unreconciled homes
with ``validation_passed: true``. A model's trade-off is text, not a decision: the
merge now reconciles the homes as if it were absent, drops the trade-off that
described the design it replaced (the trace names the dropped home), and, when the
homes cannot be reconciled, names one authoritative home and the derived copies.
No model is called anywhere here.
"""

from __future__ import annotations

import pytest

from src.agents.schema_design.dynamodb_merge import merge_overlaps
from src.agents.schema_design.group_merger import (
    OVERLAP_PREFIX,
    merge_failures,
    merge_group_drafts,
    merge_warnings,
)
from tests.unit.agents.schema_design.test_dynamodb_key_reconcile import (
    UNIQUE,
    _ap,
    _assert_every_pattern_is_served,
    _attr,
    _collection,
    _draft,
    _entity,
    _gsi_keys,
    _keys,
    _single,
)
from tests.unit.agents.schema_design.test_merge_context_tables import (
    OWNS_POSTS,
    _content_draft,
    _owner_draft,
    _placeholder_draft,
)

TERMS = "wordpress.wp_terms"
TERMMETA = "wordpress.wp_termmeta"
POSTS = "wordpress.wp_posts"
ORDERS = "shop.orders"
POST_UNIQUE = {"wp_posts": [frozenset({"id"})]}  # as in test_merge_context_tables

# What a group model wrote in the issue's run: the conflict named, nothing picked.
MODEL_TEXT = "a second, independent DynamoDB home; a migration must pick one write path"


def _second_home(source: str, *targets: str) -> dict:
    return {
        "description": f"{targets[0]} is {MODEL_TEXT}",
        "impact": "or write both and keep them in sync",
        "source_tables": [source],
        "target_tables": list(targets),
        "query_ids": [],
        "engine": "dynamodb",
    }


def _pattern_count(drafts: list[dict]) -> int:
    return sum(len(d["access_patterns"]) for d in drafts)


def _merge(drafts: list[dict], **kwargs) -> tuple[dict, list[str]]:
    notes: list[str] = []
    merged = merge_group_drafts(drafts, "dynamodb", notes=notes, **kwargs)
    # Never silently drop a statement's access pattern.
    # (Pattern IDs, and the fixture's query IDs built from them, are renumbered.)
    assert _pattern_count([merged]) == _pattern_count(drafts)
    _assert_every_pattern_is_served(merged)
    return merged, notes


# ---------------------------------------------------------------------------
# Rule 4: a single-entity table and an item-collection entity for the same rows
# ---------------------------------------------------------------------------


def _termmeta_drafts(trade_off_in: int | None = 1) -> list[dict]:
    """The issue's TermMeta vs Taxonomy shape: group 1's TermMeta maps onto TERM_META."""
    meta = _entity(
        "TERM_META",
        TERMMETA,
        "{term_id}",
        "META#{meta_id}",
        _attr("term_id", TERMMETA),
        _attr("meta_id", TERMMETA),
    )
    term = _entity("TERM", TERMS, "{term_id}", "TERM")
    g0 = _draft(
        [_collection("Taxonomy", "term_id", "sk", term, meta)],
        [_ap("DDB-AP-1", "Taxonomy", "GetItem", "PK=term_id AND sk='TERM'", source=TERMS)],
    )
    g1 = _draft(
        [
            _single(
                "TermMeta",
                TERMMETA,
                "term_id",
                "meta_id",
                attrs=[
                    _attr("term_id", TERMMETA),
                    _attr("meta_id", TERMMETA),
                    _attr("meta_value", TERMMETA, type_="S"),
                ],
            )
        ],
        [
            _ap("DDB-AP-1", "TermMeta", "Query", "PK=term_id", source=TERMMETA),
            _ap("DDB-AP-2", "TermMeta", "GetItem", "PK=term_id AND SK=meta_id", source=TERMMETA),
        ],
    )
    drafts = [g0, g1]
    if trade_off_in is not None:
        drafts[trade_off_in]["trade_offs"] = [_second_home(TERMMETA, "TermMeta", "Taxonomy")]
    return drafts


class TestFoldDespiteATradeOff:
    def test_the_single_table_folds_into_the_collection(self):
        merged, _ = _merge(_termmeta_drafts())

        [table] = merged["table_definitions"]
        assert table["table_name"] == "Taxonomy"
        [entity] = [e for e in table["entities"] if e["entity_type"] == "TERM_META"]
        assert "meta_value" in [a["name"] for a in entity["attributes"]]
        assert {ap["table_name"] for ap in merged["access_patterns"]} == {"Taxonomy"}
        assert merge_failures(merged) == [] and merge_warnings(merged) == []
        assert merged["validation_passed"] is True

    def test_the_trade_off_naming_the_dropped_home_is_removed(self):
        merged, _ = _merge(_termmeta_drafts())

        assert not [t for t in merged["trade_offs"] if MODEL_TEXT in t["description"]]
        assert not [t for t in merged["trade_offs"] if "TermMeta" in t.get("target_tables", [])]

    def test_the_trace_names_the_dropped_home_and_never_copies_the_model_text(self):
        _, notes = _merge(_termmeta_drafts())

        [dropped] = [n for n in notes if "trade-off" in n and "TermMeta" in n]
        assert "Taxonomy" in dropped and "group 1" in dropped
        assert not [n for n in notes if MODEL_TEXT in n]

    def test_whichever_group_wrote_the_trade_off_gives_the_same_design(self):
        designs = [_merge(_termmeta_drafts(trade_off_in=i))[0]["table_definitions"] for i in (0, 1)]
        assert designs[0] == designs[1] == _merge(_termmeta_drafts(None))[0]["table_definitions"]

    @pytest.mark.passes_on_main  # guard: only the trade-off about the dropped home goes
    def test_a_trade_off_about_other_tables_is_kept(self):
        drafts = _termmeta_drafts(trade_off_in=None)
        other = _second_home(TERMS, "Taxonomy", "Elsewhere")
        drafts[0]["trade_offs"] = [other]

        merged, _ = _merge(drafts)

        assert other in merged["trade_offs"]


# ---------------------------------------------------------------------------
# Rule 3: single-entity tables for the same rows with different keys
# ---------------------------------------------------------------------------


def _orders_drafts() -> list[dict]:
    """Group 0 keys orders by customer and date, group 1 by order id; group 1's
    trade-off calls its table a second home."""
    attrs = [
        _attr("order_id", ORDERS),
        _attr("customer_id", ORDERS),
        _attr("order_date", ORDERS, type_="S"),
    ]
    g0 = _draft(
        [_single("OrdersByCustomer", ORDERS, "customer_id", "order_date", attrs=attrs)],
        [_ap("DDB-AP-1", "OrdersByCustomer", "Query", "PK=customer_id AND SK begins_with :m")],
    )
    g0["table_definitions"][0]["sort_key"] = {"attribute_name": "order_date", "attribute_type": "S"}
    g1 = _draft(
        [_single("Orders", ORDERS, "order_id", attrs=attrs)],
        [
            _ap("DDB-AP-1", "Orders", "GetItem", "PK=order_id"),
            _ap("DDB-AP-2", "Orders", "UpdateItem", "PK=order_id (SET status)"),
        ],
        [_second_home(ORDERS, "Orders", "OrdersByCustomer")],
    )
    return [g0, g1]


class TestKeysUnifiedDespiteATradeOff:
    def test_one_table_keeps_the_key_most_patterns_use_and_the_other_becomes_a_gsi(self):
        merged, notes = _merge(_orders_drafts(), unique_keys=UNIQUE)

        [orders] = merged["table_definitions"]
        assert _keys(orders) == ("order_id", None)
        assert (("customer_id",), ("order_date",)) in _gsi_keys(orders).values()
        assert merge_failures(merged) == [] and merge_warnings(merged) == []
        assert not [t for t in merged["trade_offs"] if MODEL_TEXT in t["description"]]
        assert [n for n in notes if "trade-off" in n and "OrdersByCustomer" in n]


# ---------------------------------------------------------------------------
# Owner fold (#1243): a context placeholder and the owner's item collection
# ---------------------------------------------------------------------------


class TestOwnerFoldDespiteATradeOff:
    def test_the_placeholder_is_pointed_at_the_owners_table(self):
        placeholder = _placeholder_draft()
        placeholder["trade_offs"] = [_second_home(POSTS, "wp_posts", "Posts")]

        merged, notes = _merge(
            [_owner_draft(), placeholder],
            group_indices=[0, 1],
            unique_keys=POST_UNIQUE,
            owners={"wp_posts": 0},
        )

        assert [t["table_name"] for t in merged["table_definitions"]] == ["Posts"]
        assert merged["validation_passed"] is True, merged["validation_failures"]
        assert not [t for t in merged["trade_offs"] if MODEL_TEXT in t["description"]]
        assert [n for n in notes if "trade-off" in n and "wp_posts (group 1)" in n]

    def test_the_placeholder_folds_into_the_owners_collection(self):
        placeholder = _placeholder_draft()
        placeholder["trade_offs"] = [_second_home(POSTS, "wp_posts", "Content")]

        merged, _ = _merge(
            [_content_draft(), placeholder],
            group_indices=[0, 1],
            unique_keys=POST_UNIQUE,
            owners=OWNS_POSTS,
        )

        assert [t["table_name"] for t in merged["table_definitions"]] == ["Content"]
        assert merged["validation_passed"] is True, merged["validation_failures"]
        assert not [t for t in merged["trade_offs"] if MODEL_TEXT in t["description"]]


# ---------------------------------------------------------------------------
# Homes the merge cannot combine: one authoritative home, the others derived
# ---------------------------------------------------------------------------


def _posts_drafts(collection_reads: int, single_reads: int, trade_off: bool = True) -> list[dict]:
    """WpPosts (an item collection keyed by post id) and wp_posts (keyed by post_name):
    the keys do not map onto each other, so neither rule 3 nor rule 4 applies."""
    post = _entity("POST", POSTS, "{ID}", "POST", _attr("ID", POSTS), _attr("post_id", POSTS, "ID"))
    g0 = _draft(
        [_collection("WpPosts", "post_id", "sk", post)],
        [
            _ap(f"DDB-AP-{i}", "WpPosts", "GetItem", "PK=post_id AND sk='POST'", source=POSTS)
            for i in range(1, collection_reads + 1)
        ],
    )
    by_name = _single(
        "wp_posts",
        POSTS,
        "post_name",
        attrs=[_attr("post_name", POSTS, type_="S"), _attr("ID", POSTS)],
    )
    by_name["partition_key"] = {"attribute_name": "post_name", "attribute_type": "S"}
    single_aps = [
        _ap(f"DDB-AP-{i}", "wp_posts", "GetItem", "PK=post_name", source=POSTS)
        for i in range(1, single_reads + 1)
    ]
    single_aps.append(_ap("DDB-AP-90", "wp_posts", "PutItem", "PK=post_name", source=POSTS))
    g1 = _draft(
        [by_name],
        single_aps,
        [_second_home(POSTS, "wp_posts", "WpPosts")] if trade_off else None,
    )
    return [g0, g1]


class TestUnreconcilableHomes:
    def test_a_trade_off_no_longer_hides_the_review(self):
        merged, _ = _merge(_posts_drafts(1, 2))

        [overlap] = merge_overlaps(merged)
        assert overlap["description"].startswith(OVERLAP_PREFIX)
        assert sorted(overlap["target_tables"]) == ["WpPosts", "wp_posts"]
        assert merged["validation_passed"] is True

    def test_the_home_serving_the_most_statements_is_authoritative(self):
        [by_single] = merge_overlaps(_merge(_posts_drafts(1, 2))[0])
        [by_collection] = merge_overlaps(_merge(_posts_drafts(3, 1))[0])

        assert "keeps wp_posts as the authoritative home" in by_single["description"]
        assert "WpPosts as a derived copy" in by_single["description"]
        assert "keeps WpPosts as the authoritative home" in by_collection["description"]
        assert "wp_posts as a derived copy" in by_collection["description"]
        for overlap in (by_single, by_collection):
            assert "DynamoDB Streams" in overlap["impact"]

    def test_statements_come_before_design_rps(self):
        drafts = _posts_drafts(1, 2)
        drafts[0]["access_patterns"][0]["design_rps"] = 500.0  # one busy statement

        [overlap] = merge_overlaps(_merge(drafts)[0])

        assert "keeps wp_posts as the authoritative home" in overlap["description"]

    def test_equal_statements_go_to_the_higher_design_rps(self):
        def merged_with(collection_rps: float) -> dict:
            drafts = _posts_drafts(2, 1)
            for ap in drafts[0]["access_patterns"]:
                ap["design_rps"] = collection_rps
            return merge_overlaps(_merge(drafts)[0])[0]

        assert "keeps WpPosts as the authoritative home" in merged_with(50.0)["description"]
        assert "keeps wp_posts as the authoritative home" in merged_with(0.1)["description"]

    def test_the_choice_ignores_draft_order(self):
        drafts = _posts_drafts(1, 2)
        forward = merge_overlaps(_merge(drafts)[0])
        backward = merge_overlaps(_merge(drafts[::-1], group_indices=[1, 0])[0])
        assert forward and forward == backward

    def test_a_write_on_the_derived_copy_is_pointed_at_the_authoritative_home(self):
        merged, _ = _merge(_posts_drafts(3, 1))

        [put] = [ap for ap in merged["access_patterns"] if ap["operation"] == "PutItem"]
        assert put["table_name"] == "wp_posts"  # never moved, never dropped
        assert "write WpPosts" in put["key_condition"]
        assert "DynamoDB Streams" in put["key_condition"]

    def test_the_review_names_the_choice_with_or_without_a_trade_off(self):
        with_text = merge_overlaps(_merge(_posts_drafts(1, 2))[0])
        without = merge_overlaps(_merge(_posts_drafts(1, 2, trade_off=False))[0])
        assert [o["description"] for o in with_text] == [o["description"] for o in without]
