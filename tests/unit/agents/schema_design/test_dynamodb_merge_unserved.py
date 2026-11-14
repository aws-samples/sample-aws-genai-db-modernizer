# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The access patterns an unresolved DynamoDB merge leaves unserved (#802).

Every ``DynamoDB merge:`` failure is listed in ``unresolved_merge_conflicts`` with
what its access patterns are (``status``):

* unserved: a dropped design's patterns, now on the kept table; a pattern on a table
  or GSI the design does not define;
* at risk: the patterns on a table the failure says cannot be built as designed (more
  GSIs than the limit, a name over the limit), on the tables holding an entity with
  contradictory keys, or on the rows an ownership failure left with no safe key;
* none named: a failure that names no access pattern (a dangling hot-partition entry);
* not recorded: a design merged before the field, whose unserved patterns are unknown.

A clean merge lists none.
"""

from __future__ import annotations

import pytest

from src.agents.schema_design.dynamodb_merge import (
    AT_RISK,
    MERGE_FAILURE_PREFIX,
    NONE_NAMED,
    NOT_RECORDED,
    UNSERVED,
    merge_failures,
    unresolved_merge_conflicts,
)
from src.agents.schema_design.group_merger import merge_group_drafts, merge_schema_groups
from src.agents.schema_design.pattern_input import complete_pattern_members
from src.contracts.dynamodb_model_output import DynamoDBModelOutputContract
from src.storage.local_store import LocalArtifactStore
from tests.unit.agents.schema_design.test_merge_context_tables import (
    OWNS_POSTS,
    POSTS,
    UNIQUE,
    _content_draft,
    _placeholder_draft,
    _post_entity,
)
from tests.unit.agents.schema_design.valid_designs import (
    ddb_attr,
    ddb_design,
    ddb_pattern,
    ddb_table,
)


def _main(source: str, pk: str, *patterns: tuple[str, str]) -> dict:
    """A draft with one table ``Main`` holding ``source``'s rows, read by ``patterns``."""
    return ddb_design(
        [ddb_table("Main", source, pk=pk)],
        [ddb_pattern(pid, "Main", source, query) for pid, query in patterns],
    )


def _drafts() -> list[dict]:
    # Two groups give the name Main to two different designs: the orders design (two
    # patterns) is kept, the users design (one pattern) is dropped.
    users = _main("users", "user_id", ("DDB-AP-1", "q-users-by-id"))
    orders = _main(
        "orders", "order_id", ("DDB-AP-1", "q-order-by-id"), ("DDB-AP-2", "q-order-lines")
    )
    return [users, orders]


def _dropped_merge() -> dict:
    return merge_group_drafts(_drafts(), "dynamodb", group_indices=[1, 2])


def _users_pattern(merged: dict) -> str:
    return next(  # type: ignore[no-any-return]
        ap["pattern_id"] for ap in merged["access_patterns"] if "q-users-by-id" in ap["query_ids"]
    )


def _gsis(n: int) -> list[dict]:
    return [
        {
            "gsi_name": f"G{i}",
            "partition_key": [{"attribute_name": f"a{i}", "attribute_type": "S"}],
            "projection": "KEYS_ONLY",
            "item_count": 10,
            "item_size_bytes": 50,
        }
        for i in range(n)
    ]


class TestADroppedDesign:
    def test_lists_the_dropped_designs_patterns_and_queries_as_unserved(self):
        merged = _dropped_merge()

        [failure] = merge_failures(merged)
        assert merged["unresolved_merge_conflicts"] == [
            {
                "failure": failure,
                "status": UNSERVED,
                "table_name": "Main",
                "pattern_ids": [_users_pattern(merged)],
                "query_ids": ["q-users-by-id"],
            }
        ]
        assert [t["source_tables"] for t in merged["table_definitions"]] == [["orders"]]

    def test_the_merged_design_passes_its_output_contract(self):
        parsed = DynamoDBModelOutputContract.model_validate(_dropped_merge())

        [conflict] = parsed.unresolved_merge_conflicts
        assert (conflict.status, conflict.query_ids) == (UNSERVED, ["q-users-by-id"])

    def test_the_reader_returns_the_stored_list(self):
        merged = _dropped_merge()

        assert unresolved_merge_conflicts(merged) == merged["unresolved_merge_conflicts"]

    def test_the_reader_skips_a_failure_a_hand_fix_cleared(self):
        merged = _dropped_merge()
        merged["validation_failures"] = []

        assert unresolved_merge_conflicts(merged) == []


class TestQueriesAddedAfterTheMerge:
    """``complete_pattern_members`` (#1602) adds a query pattern's other members to the
    access pattern that serves one of them, after the merge recorded the conflict."""

    def test_the_reader_lists_the_added_members(self):
        completed, _ = complete_pattern_members(
            _dropped_merge(), [["q-users-by-id", "q-users-by-id-2"]]
        )

        [conflict] = unresolved_merge_conflicts(completed)

        assert conflict["query_ids"] == ["q-users-by-id", "q-users-by-id-2"]

    def test_the_written_schema_output_lists_them(self, tmp_path):
        base = "shop/job-1/schema-dynamodb/v1"
        store = LocalArtifactStore(str(tmp_path))
        groups = [
            {
                "group_index": i,
                "group_name": f"g{i}",
                "primary_tables": [source],
                "query_count": 1,
                "table_count": 1,
                "input_file": f"input_group_{i}.json",
                "input_pages": [],
            }
            for i, source in enumerate(("users", "orders"))
        ]
        store.write_json(
            f"{base}/groups_manifest.json",
            {
                "job_id": "job-1",
                "database_name": "shop",
                "target_engine": "dynamodb",
                "total_queries": 4,
                "total_groups": 2,
                "groups": groups,
            },
        )
        for i, draft in enumerate(_drafts()):
            store.write_json(f"{base}/schema_draft_group_{i}.json", draft)
        store.write_json(
            f"{base}/input_group_0.json",
            {
                "collector_output": {
                    "queries": {
                        "query_pattern_groups": [
                            {"query_ids": ["q-users-by-id", "q-users-by-id-2"]}
                        ]
                    }
                }
            },
        )

        merge_schema_groups("job-1", "shop", "dynamodb", store)

        written = store.read_json(f"{base}/schema_output.json")
        [conflict] = written["unresolved_merge_conflicts"]
        assert conflict["query_ids"] == ["q-users-by-id", "q-users-by-id-2"]


class TestDanglingReferences:
    def test_a_pattern_on_a_missing_gsi_is_unserved(self):
        draft = ddb_design(
            [ddb_table("Users", "users")],
            [ddb_pattern("DDB-AP-1", "Users", "users", "q-by-email", gsi_name="ByEmail")],
        )
        other = ddb_design(
            [ddb_table("Orders", "orders")],
            [ddb_pattern("DDB-AP-1", "Orders", "orders", "q-order")],
        )

        merged = merge_group_drafts([draft, other], "dynamodb")

        [conflict] = merged["unresolved_merge_conflicts"]
        assert conflict["failure"].startswith(MERGE_FAILURE_PREFIX)
        assert "GSI 'ByEmail'" in conflict["failure"]
        assert (conflict["status"], conflict["table_name"]) == (UNSERVED, "Users")
        assert conflict["query_ids"] == ["q-by-email"]
        [pid] = conflict["pattern_ids"]
        assert pid in conflict["failure"]

    def test_a_hot_partition_entry_on_a_missing_table_names_no_pattern(self):
        draft = ddb_design(
            [ddb_table("Users", "users")], [ddb_pattern("DDB-AP-1", "Users", "users", "q-1")]
        )
        draft["hot_partition_analysis"].append({"table_name": "Gone"})

        merged = merge_group_drafts([draft], "dynamodb")

        [conflict] = merged["unresolved_merge_conflicts"]
        assert "hot-partition entry" in conflict["failure"]
        assert conflict["status"] == NONE_NAMED
        assert (conflict["pattern_ids"], conflict["query_ids"]) == ([], [])

    def test_identical_failures_keep_their_own_queries(self):
        """Two patterns with no ID on one missing table give two identical failure
        texts; each entry keeps its own pattern's queries (paired by position)."""
        draft = ddb_design(
            [ddb_table("Users", "users")],
            [
                ddb_pattern("", "Gone", "users", "q-1"),
                ddb_pattern("", "Gone", "users", "q-2"),
            ],
        )

        merged = merge_group_drafts([draft], "dynamodb")

        conflicts = merged["unresolved_merge_conflicts"]
        assert [c["failure"] for c in conflicts] == [conflicts[0]["failure"]] * 2
        assert [c["query_ids"] for c in conflicts] == [["q-1"], ["q-2"]]


class TestATableThatCannotBeBuiltAsDesigned:
    def test_more_gsis_than_the_limit_put_the_tables_patterns_at_risk(self):
        draft = ddb_design(
            [ddb_table("Users", "users", gsis=_gsis(21)), ddb_table("Orders", "orders")],
            [
                ddb_pattern("DDB-AP-1", "Users", "users", "q-user"),
                ddb_pattern("DDB-AP-2", "Orders", "orders", "q-order"),
            ],
        )

        merged = merge_group_drafts([draft], "dynamodb")

        [failure] = merge_failures(merged)
        assert "21 GSIs" in failure
        assert merged["unresolved_merge_conflicts"] == [
            {
                "failure": failure,
                "status": AT_RISK,
                "table_name": "Users",
                "pattern_ids": ["DDB-AP-1"],
                "query_ids": ["q-user"],
            }
        ]

    def test_a_table_name_over_the_limit_puts_its_patterns_at_risk(self):
        long = "T" * 256
        draft = ddb_design(
            [ddb_table(long, "users")], [ddb_pattern("DDB-AP-1", long, "users", "q-user")]
        )

        merged = merge_group_drafts([draft], "dynamodb")

        [conflict] = merged["unresolved_merge_conflicts"]
        assert "characters, over the DynamoDB limit" in conflict["failure"]
        assert (conflict["status"], conflict["query_ids"]) == (AT_RISK, ["q-user"])

    def test_a_gsi_name_over_the_limit_puts_the_tables_patterns_at_risk(self):
        gsi = {**_gsis(1)[0], "gsi_name": "G" * 256}
        draft = ddb_design(
            [ddb_table("Users", "users", gsis=[gsi])],
            [ddb_pattern("DDB-AP-1", "Users", "users", "q-user")],
        )

        merged = merge_group_drafts([draft], "dynamodb")

        [conflict] = merged["unresolved_merge_conflicts"]
        assert conflict["failure"].startswith(f"{MERGE_FAILURE_PREFIX}GSI name")
        assert (conflict["status"], conflict["table_name"]) == (AT_RISK, "Users")

    def test_a_failing_table_with_no_pattern_names_none(self):
        draft = ddb_design(
            [ddb_table("Users", "users", gsis=_gsis(21)), ddb_table("Orders", "orders")],
            [ddb_pattern("DDB-AP-1", "Orders", "orders", "q-order")],
        )

        merged = merge_group_drafts([draft], "dynamodb")

        [conflict] = merged["unresolved_merge_conflicts"]
        assert (conflict["status"], conflict["pattern_ids"]) == (NONE_NAMED, [])


class TestContradictoryEntityKeys:
    def test_the_patterns_on_the_entitys_rows_are_at_risk(self):
        def collection(name: str, sk: str, query: str) -> dict:
            entity = {
                "entity_type": "USER",
                "source_table": "users",
                "pk_template": "{id}",
                "sk_template": sk,
                "attributes": [ddb_attr("id", "users")],
            }
            table = ddb_table(
                name,
                "users",
                aggregate_pattern="item_collection",
                sort_key={"attribute_name": "sk", "attribute_type": "S"},
                entities=[entity],
            )
            return ddb_design([table], [ddb_pattern("DDB-AP-1", name, "users", query)])

        merged = merge_group_drafts(
            [collection("A", "USER", "q-a"), collection("B", "PROFILE#{id}", "q-b")], "dynamodb"
        )

        [conflict] = merged["unresolved_merge_conflicts"]
        assert "contradictory key templates" in conflict["failure"]
        assert conflict["status"] == AT_RISK
        assert sorted(conflict["query_ids"]) == ["q-a", "q-b"]
        assert conflict["table_name"] is None  # two tables


class TestOwnershipFailures:
    def test_a_placeholder_gsi_on_another_entity_column_puts_its_rows_at_risk(self):
        post = _post_entity("POST#{post_id}")
        post["attributes"].append(ddb_attr("post_author", POSTS, "post_title"))

        merged = merge_group_drafts(
            [_content_draft(post), _placeholder_draft()],
            "dynamodb",
            group_indices=[0, 1],
            unique_keys=UNIQUE,
            owners=OWNS_POSTS,
        )

        [conflict] = [
            c for c in merged["unresolved_merge_conflicts"] if "another column" in c["failure"]
        ]
        assert conflict["status"] == AT_RISK
        assert "q2" in conflict["query_ids"]

    def test_a_placeholder_the_owner_cannot_be_shown_to_serve_puts_its_rows_at_risk(self):
        """The owner keys topics on ``C#{category_id}``, a column the context
        placeholder lacks (the #855 case)."""
        from tests.unit.agents.schema_design import test_dynamodb_key_reconcile as kr

        entity = kr._entity(
            "TOPIC",
            kr.TOPICS,
            "{id}",
            "C#{category_id}",
            kr._attr("id", kr.TOPICS),
            kr._attr("category_id", kr.TOPICS),
        )
        drafts = [
            kr._draft(
                [kr._collection("topic_posts", "pk", "sk", entity)],
                [kr._topic_read("DDB-AP-1", "topic_posts")],
            ),
            kr._draft(
                [kr._single("topics", kr.TOPICS, "id")],
                [kr._ap("DDB-AP-1", "topics", "GetItem", "PK=id", source=kr.TOPICS)],
            ),
        ]

        merged = merge_group_drafts(drafts, "dynamodb", [0, 1], owners={"topics": 0})

        [conflict] = [
            c for c in merged["unresolved_merge_conflicts"] if "owner group" in c["failure"]
        ]
        assert conflict["status"] == AT_RISK
        assert len(conflict["pattern_ids"]) == 2  # the rows' patterns on both tables

    def test_an_owner_key_not_safe_for_every_group_puts_its_rows_at_risk(self):
        """The owner keys topics on ``slug``; another group's table writes items by
        ``id``, which ``slug`` would not identify: no key is kept for the rows."""
        owner = ddb_design(
            [ddb_table("TopicsBySlug", "topics", pk="slug")],
            [ddb_pattern("DDB-AP-1", "TopicsBySlug", "topics", "q-slug")],
        )
        other = ddb_design(
            [ddb_table("Topics", "topics", pk="id")],
            [ddb_pattern("DDB-AP-1", "Topics", "topics", "q-id")],
        )

        merged = merge_group_drafts([owner, other], "dynamodb", [0, 1], owners={"topics": 0})

        [conflict] = [
            c for c in merged["unresolved_merge_conflicts"] if "so no key is kept" in c["failure"]
        ]
        assert conflict["status"] == AT_RISK
        assert conflict["query_ids"] == ["q-id", "q-slug"]  # every pattern on the rows


class TestACleanMerge:
    @pytest.mark.passes_on_main  # a clean merge already writes no failure
    def test_lists_none(self):
        users = ddb_design(
            [ddb_table("Users", "users")], [ddb_pattern("DDB-AP-1", "Users", "users", "q-1")]
        )
        orders = ddb_design(
            [ddb_table("Orders", "orders")], [ddb_pattern("DDB-AP-1", "Orders", "orders", "q-2")]
        )

        merged = merge_group_drafts([users, orders], "dynamodb")

        assert merge_failures(merged) == []
        assert "unresolved_merge_conflicts" not in merged
        assert unresolved_merge_conflicts(merged) == []

    def test_a_stale_list_copied_from_an_earlier_merge_is_dropped(self):
        stale = _dropped_merge()
        users = ddb_design(
            [ddb_table("Users", "users")], [ddb_pattern("DDB-AP-1", "Users", "users", "q-1")]
        )
        users["unresolved_merge_conflicts"] = stale["unresolved_merge_conflicts"]
        users["validation_failures"] = stale["validation_failures"]
        users["validation_passed"] = False
        orders = ddb_design(
            [ddb_table("Orders", "orders")], [ddb_pattern("DDB-AP-1", "Orders", "orders", "q-2")]
        )

        merged = merge_group_drafts([users, orders], "dynamodb")

        assert merged["validation_passed"] is True
        assert unresolved_merge_conflicts(merged) == []
        assert "unresolved_merge_conflicts" not in merged


class TestADesignWrittenBeforeTheField:
    """A schema output saved before #802 has failure strings only."""

    def _legacy(self) -> dict:
        merged = _dropped_merge()
        del merged["unresolved_merge_conflicts"]
        return merged

    def test_still_passes_its_output_contract(self):
        parsed = DynamoDBModelOutputContract.model_validate(self._legacy())

        assert parsed.unresolved_merge_conflicts == []

    def test_the_reader_lists_each_failure_as_not_recorded(self):
        """What such a failure leaves unserved is unknown, never "none"."""
        legacy = self._legacy()

        assert unresolved_merge_conflicts(legacy) == [
            {
                "failure": f,
                "status": NOT_RECORDED,
                "table_name": None,
                "pattern_ids": [],
                "query_ids": [],
            }
            for f in merge_failures(legacy)
        ]

    def test_an_entry_without_a_status_reads_by_its_patterns(self):
        merged = _dropped_merge()
        for c in merged["unresolved_merge_conflicts"]:
            del c["status"]

        [conflict] = unresolved_merge_conflicts(merged)

        assert conflict["status"] == UNSERVED

    def test_a_design_with_no_merge_failure_lists_none(self):
        assert unresolved_merge_conflicts({"validation_failures": ["own check"]}) == []
        assert unresolved_merge_conflicts(None) == []
