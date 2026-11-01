# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The stream sync of DynamoDB derived copies is listed and priced (#1527).

#333 gave a source table one authoritative DynamoDB home and turned its other homes
into derived copies kept in sync from the home's stream. ``derived_copy_sync_facts``
lists each sync (source table, home, copies, writes a second that must be replayed)
and prices it: DynamoDB Streams read request units (one unit per GetRecords call of up
to 1 MB) plus the copies' write request units, each replayed item rounded up to 1 KB
(the unit rules of the DynamoDB estimate, #1491), at the price table's on-demand prices.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from src.agents.referee.derived_copy_sync import derived_copy_sync_facts
from src.agents.schema_design.dynamodb_merge import OVERLAP_PREFIX, overlap_home
from src.agents.schema_design.group_merger import merge_group_drafts
from src.shared import catalog
from src.shared.catalog.prices import SECONDS_PER_MONTH
from tests.unit.agents.schema_design.test_dynamodb_merge_conflicts import _posts_drafts

WRITE_PRICE = 0.625
STREAMS_PRICE = 0.2


def _rates(schema: dict) -> dict:
    """A collection whose queries write, on average, at the rate each fixture pattern
    names in ``design_rps`` (#1753: the sync reads the collection, never ``design_rps``;
    the tests below that tell the two apart give the collection its own rate)."""
    rates: dict[str, float] = {}
    for ap in schema.get("access_patterns") or []:
        for q in ap.get("query_ids") or []:
            rates.setdefault(q, float(ap.get("design_rps") or 0.0))
    return {
        "queries": {
            "query_patterns": [
                {"query_id": q, "query_type": "INSERT", "calls_per_second": cps}
                for q, cps in rates.items()
            ]
        }
    }


def _facts(schema, risks):  # type: ignore[no-untyped-def]
    return derived_copy_sync_facts(schema, risks, _rates(schema or {}))


def _ap(table: str, operation: str, rps: float, size: int, *, qid: str, source="wp_posts") -> dict:
    return {
        "pattern_id": f"AP-{qid}",
        "query_ids": [qid],
        "source_tables": [source],
        "operation": operation,
        "table_name": table,
        "design_rps": rps,
        "item_size_bytes": size,
    }


def _overlap(targets: list[str], sources=("wp_posts",)) -> dict:
    return {
        "description": f"{OVERLAP_PREFIX}{', '.join(sources)} modelled independently",
        "impact": "...",
        "source_tables": list(sources),
        "target_tables": targets,
        "query_ids": [],
        "engine": "dynamodb",
    }


def _risk(rid="RISK-004", sources=("wp_posts",)) -> dict:
    return {
        "risk_id": rid,
        "risk_type": "DATA_CONSISTENCY",
        "severity": "MEDIUM",
        "description": "[dynamodb] wp_posts modelled independently",
        "affected_tables": sorted(sources),
    }


def _schema(aps: list[dict], targets=("WpPosts", "wp_posts")) -> dict:
    # WpPosts serves three statements, so it is the authoritative home.
    reads = [_ap("WpPosts", "GetItem", 9.0, 10, qid=f"r{i}") for i in range(3)]
    return {
        "trade_offs": [_overlap(list(targets))],
        "access_patterns": reads + aps,
    }


class TestTheListing:
    def test_lists_the_source_table_the_home_the_copies_and_the_replayed_writes(self):
        put = _ap("wp_posts", "PutItem", 4.0, 1500, qid="w1")
        [sync] = _facts(_schema([put]), [_risk()])["syncs"]

        assert sync["risk_id"] == "RISK-004"
        assert sync["source_tables"] == ["wp_posts"]
        assert sync["authoritative_home"] == "WpPosts"
        assert [c["table"] for c in sync["derived"]] == ["wp_posts"]
        assert sync["replayed_writes_per_second"] == 4.0

    def test_writes_on_every_home_are_replayed_reads_are_not(self):
        aps = [
            _ap("wp_posts", "PutItem", 4.0, 100, qid="w1"),
            _ap("WpPosts", "UpdateItem", 1.5, 100, qid="w2"),
            _ap("wp_posts", "Query", 50.0, 100, qid="r9"),
        ]
        [sync] = _facts(_schema(aps), [_risk()])["syncs"]

        assert sync["replayed_writes_per_second"] == 5.5

    def test_writes_to_other_source_tables_are_not_replayed(self):
        aps = [
            _ap("wp_posts", "PutItem", 4.0, 100, qid="w1"),
            _ap("WpPosts", "PutItem", 7.0, 100, qid="w2", source="wp_users"),
        ]
        [sync] = _facts(_schema(aps), [_risk()])["syncs"]

        assert sync["replayed_writes_per_second"] == 4.0

    def test_the_home_is_the_one_the_merge_names(self):
        merged = merge_group_drafts(_posts_drafts(3, 1), "dynamodb", notes=[])
        [overlap] = [t for t in merged["trade_offs"] if t["description"].startswith(OVERLAP_PREFIX)]
        for ap in merged["access_patterns"]:
            ap["design_rps"] = 2.0
        [sync] = _facts(merged, [_risk()])["syncs"]

        assert sync["authoritative_home"] == "WpPosts"
        assert "keeps WpPosts as the authoritative home" in overlap["description"]
        assert overlap_home(overlap, merged["access_patterns"]) == ("WpPosts", ["wp_posts"])

    def test_no_overlap_no_fact(self):
        assert _facts({"trade_offs": [], "access_patterns": []}, []) is None
        assert _facts(None, [_risk()]) is None

    def test_an_overlap_with_no_open_risk_is_not_listed(self):
        put = _ap("wp_posts", "PutItem", 4.0, 100, qid="w1")
        assert _facts(_schema([put]), []) is None
        assert _facts(_schema([put]), [_risk(sources=("wp_users",))]) is None

    def test_a_copy_with_no_writes_still_lists_its_sync_at_zero(self):
        [sync] = _facts(_schema([]), [_risk()])["syncs"]

        assert sync["replayed_writes_per_second"] == 0.0
        assert sync["monthly_cost_usd"] == 0.0
        assert [c["write_units_per_month"] for c in sync["derived"]] == [0.0]


class TestThePrice:
    def test_copy_writes_round_each_item_up_to_1_kb_at_the_on_demand_write_price(self):
        put = _ap("wp_posts", "PutItem", 4.0, 1500, qid="w1")  # 1500 B = 2 write units
        [sync] = _facts(_schema([put]), [_risk()])["syncs"]

        units = 4.0 * 2 * SECONDS_PER_MONTH
        [copy] = sync["derived"]
        assert copy["write_units_per_month"] == units
        assert copy["cost_usd"] == round(units / 1e6 * WRITE_PRICE, 2)
        assert sync["copy_write_cost_usd"] == copy["cost_usd"]

    def test_stream_reads_bill_one_unit_per_started_mb_of_replayed_changes(self):
        put = _ap("wp_posts", "PutItem", 4.0, 1500, qid="w1")
        [sync] = _facts(_schema([put]), [_risk()])["syncs"]

        units = math.ceil(4.0 * 1500 * SECONDS_PER_MONTH / 1_048_576)
        assert sync["stream_read_units_per_month"] == units
        assert sync["stream_read_cost_usd"] == round(units / 1e6 * STREAMS_PRICE, 2)

    def test_a_busy_table_makes_the_stream_cost_visible(self):
        put = _ap("wp_posts", "PutItem", 5000.0, 4096, qid="w1")
        [sync] = _facts(_schema([put]), [_risk()])["syncs"]

        assert sync["stream_read_cost_usd"] > 0
        assert sync["monthly_cost_usd"] == round(
            sync["stream_read_cost_usd"] + sync["copy_write_cost_usd"], 2
        )

    def test_the_stream_is_read_once_but_every_copy_is_written(self):
        aps = [
            _ap("wp_posts", "PutItem", 4.0, 1500, qid="w1"),
            _ap("wp_posts_b", "PutItem", 2.0, 1500, qid="w2"),
        ]
        schema = _schema(aps, targets=("WpPosts", "wp_posts", "wp_posts_b"))
        facts = _facts(schema, [_risk()])
        [sync] = facts["syncs"]

        assert [c["table"] for c in sync["derived"]] == ["wp_posts", "wp_posts_b"]
        assert sync["replayed_writes_per_second"] == 6.0
        # Every copy replays all six writes a second: each costs the same.
        a, b = sync["derived"]
        assert a["write_units_per_month"] == b["write_units_per_month"]
        assert a["write_units_per_month"] == 6.0 * 2 * SECONDS_PER_MONTH
        assert sync["stream_read_units_per_month"] == math.ceil(
            6.0 * 1500 * SECONDS_PER_MONTH / 1_048_576
        )
        assert facts["total_monthly_cost_usd"] == sync["monthly_cost_usd"]

    def test_a_missing_streams_price_raises(self, monkeypatch):
        real = catalog.pricing("dynamodb")
        monkeypatch.setattr(
            catalog,
            "pricing",
            lambda engine: real.model_copy(update={"streams_read_unit_price_per_million": None}),
        )
        put = _ap("wp_posts", "PutItem", 4.0, 100, qid="w1")
        with pytest.raises(ValueError, match="streams_read_unit_price_per_million"):
            _facts(_schema([put]), [_risk()])

    def test_states_the_unit_rules_it_priced_on(self):
        put = _ap("wp_posts", "PutItem", 4.0, 100, qid="w1")
        facts: dict[str, Any] = _facts(_schema([put]), [_risk()])
        text = " ".join(facts["assumptions"])

        assert "1 KB" in text and "1 MB" in text and "GetRecords" in text
        assert facts["prices"] == {
            "streams_read_unit_price_per_million": STREAMS_PRICE,
            "write_unit_price_per_million": WRITE_PRICE,
        }


class TestTheSentences:
    """Written once with the fact (ADR-032); every deliverable prints these."""

    def _sync(self) -> dict:
        aps = [
            _ap("wp_posts", "PutItem", 4.0, 1500, qid="w1"),
            _ap("wp_posts_b", "PutItem", 2.0, 1500, qid="w2"),
        ]
        schema = _schema(aps, targets=("WpPosts", "wp_posts", "wp_posts_b"))
        facts = _facts(schema, [_risk()])
        assert facts is not None
        sync: dict = facts["syncs"][0]
        return sync

    def test_the_path_names_the_source_the_home_and_the_copies(self):
        path = self._sync()["path_line"]

        assert "writes to wp_posts go to WpPosts" in path
        assert "wp_posts, wp_posts_b are derived copies" in path
        assert "DynamoDB Streams of WpPosts" in path
        assert "6 writes per second" in path

    def test_the_cost_states_both_parts_and_each_copy(self):
        cost = self._sync()["cost_line"]

        assert cost.startswith("Sync cost: $39.42/mo at on-demand prices")
        assert "22,557 DynamoDB Streams read request units" in cost
        assert "wp_posts $19.71" in cost and "wp_posts_b $19.71" in cost

    def test_the_summary_names_no_table(self):
        summary = self._sync()["summary_line"]

        assert "wp_posts" not in summary and "WpPosts" not in summary
        assert "2 derived copies are kept in sync" in summary
        assert "$39.42 a month" in summary

    def test_one_copy_reads_in_the_singular(self):
        put = _ap("wp_posts", "PutItem", 4.0, 100, qid="w1")
        [sync] = _facts(_schema([put]), [_risk()])["syncs"]

        assert "wp_posts is a derived copy" in sync["path_line"]
        assert sync["summary_line"].startswith("1 derived copy is kept in sync")


class TestTheRate:
    """#1753: the sync is priced on the collected average load, as the GSI cost is."""

    def _collector(self, cps: float, rows: float | None = None, qid: str = "w1") -> dict:
        query: dict[str, Any] = {"query_id": qid, "query_type": "INSERT", "calls_per_second": cps}
        if rows is not None:
            query["rows_affected_avg"] = rows
        return {"queries": {"query_patterns": [query]}}

    def test_the_rate_is_calls_per_second_times_items_never_design_rps(self):
        put = _ap("wp_posts", "PutItem", 999.0, 1500, qid="w1")  # design_rps: peak x growth
        facts = derived_copy_sync_facts(_schema([put]), [_risk()], self._collector(2.0, 3))
        assert facts is not None
        [sync] = facts["syncs"]

        assert sync["replayed_writes_per_second"] == 6.0
        assert sync["derived"][0]["write_units_per_month"] == 6.0 * 2 * SECONDS_PER_MONTH

    def test_no_collection_replays_nothing(self):
        put = _ap("wp_posts", "PutItem", 999.0, 1500, qid="w1")
        [sync] = derived_copy_sync_facts(_schema([put]), [_risk()])["syncs"]

        assert sync["replayed_writes_per_second"] == 0.0

    def test_only_the_engines_in_scope_queries_replay(self):
        put = _ap("wp_posts", "PutItem", 4.0, 1500, qid="w1")
        collector = self._collector(2.0)
        [kept] = derived_copy_sync_facts(_schema([put]), [_risk()], collector, ["w1"])["syncs"]
        [none] = derived_copy_sync_facts(_schema([put]), [_risk()], collector, [])["syncs"]

        assert kept["replayed_writes_per_second"] == 2.0
        assert none["replayed_writes_per_second"] == 0.0

    def test_a_query_in_two_write_patterns_replays_once(self):
        aps = [
            _ap("wp_posts", "PutItem", 4.0, 1500, qid="w1"),
            _ap("WpPosts", "UpdateItem", 4.0, 1500, qid="w1"),
        ]
        facts = derived_copy_sync_facts(_schema(aps), [_risk()], self._collector(2.0))
        assert facts is not None

        assert facts["syncs"][0]["replayed_writes_per_second"] == 2.0

    def test_the_sync_and_the_gsi_cost_read_the_same_rate(self):
        """One write stream, two design-time costs: the copies replay exactly the items
        a second the home's GSI is written for (same item size, so the same units)."""
        from src.agents.referee.gsi_cost import gsi_cost_facts

        put = _ap("WpPosts", "PutItem", 999.0, 1500, qid="w1")
        schema = _schema([put])
        schema["table_definitions"] = [
            {
                "table_name": "WpPosts",
                "source_tables": ["wp_posts"],
                "partition_key": {"attribute_name": "id", "attribute_type": "S"},
                "attributes": [{"name": n, "type": "S"} for n in ("id", "post_author")],
                "gsis": [
                    {
                        "gsi_name": "ByAuthor",
                        "partition_key": [{"attribute_name": "post_author", "attribute_type": "S"}],
                        "projection": "ALL",
                        "item_count": 10,
                        "item_size_bytes": 1500,
                    }
                ],
                "item_count": 10,
                "item_size_bytes": 1500,
            },
        ]
        collector = self._collector(2.5, 2)
        facts = derived_copy_sync_facts(schema, [_risk()], collector, ["w1"])
        gsi = gsi_cost_facts(schema, collector, ["w1"], "dynamodb")
        assert facts is not None and gsi is not None
        [sync] = facts["syncs"]

        assert sync["replayed_writes_per_second"] == 5.0
        assert sync["derived"][0]["write_units_per_month"] == gsi["write_units_per_month"]
        assert gsi["write_units_per_month"] == 5.0 * 2 * SECONDS_PER_MONTH

    def test_a_fraction_of_a_row_per_call_stays_a_fraction(self):
        """A write that usually changes no row (0.0156 rows a call) replays 0.0156 items
        a call, never one: the collected rows per call, as they are."""
        put = _ap("wp_posts", "PutItem", 999.0, 1500, qid="w1")
        facts = derived_copy_sync_facts(_schema([put]), [_risk()], self._collector(8.0, 0.0156))
        assert facts is not None

        [sync] = facts["syncs"]
        assert sync["derived"][0]["write_units_per_month"] == pytest.approx(
            8.0 * 0.0156 * 2 * SECONDS_PER_MONTH
        )

    def test_an_average_of_exactly_zero_rows_counts_as_one_row(self):
        """0.0 rows on every call is far likelier missing collector data than real zero
        work: it counts as one row a call, as no row statistics do."""
        put = _ap("wp_posts", "PutItem", 999.0, 1500, qid="w1")
        zero = derived_copy_sync_facts(_schema([put]), [_risk()], self._collector(8.0, 0.0))
        missing = derived_copy_sync_facts(_schema([put]), [_risk()], self._collector(8.0))
        assert zero is not None and missing is not None

        assert zero["syncs"][0]["replayed_writes_per_second"] == 8.0
        assert zero["syncs"] == missing["syncs"]
