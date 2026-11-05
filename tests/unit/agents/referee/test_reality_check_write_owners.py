# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The reality-check summary states who carries the writes as the assignment has it (#1928).

Two full assessments of the bundled WordPress sample put every write on DynamoDB, with
Aurora MySQL reading a copy kept in sync by change data capture. The summary at the
approval gate said the reverse in both runs, and validation let it through: the
model's input named Aurora MySQL as the engine that "takes every write" (the CQRS
pattern was detected without the write groups) and carried no write owner, and the
summary check looked only at kept and eliminated engines.

The fixture below is the shape of those runs, cut down: DynamoDB carries every write
on ``wp_options`` and ``wp_postmeta``, Aurora MySQL reads both and owns the read-only
``wp_posts``.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from src.agents.referee.reality_check_handler import prepare_reality_check_llm_input
from src.agents.referee.reality_check_patterns import (
    _detect_architectural_patterns,
    patterns_for_assignment,
)
from src.agents.referee.reality_check_summary import (
    check_summary_write_claims,
    finalize_executive_summary,
)
from src.agents.referee.reality_check_writes import write_ownership

OPTIONS, POSTMETA, POSTS = "wordpress.wp_options", "wordpress.wp_postmeta", "wordpress.wp_posts"


def _qa(qid: str, engine: str, *tables: str) -> dict:
    return {
        "query_id": qid,
        "assigned_engine": engine,
        "in_scope": True,
        "source_tables": list(tables),
    }


def _qas() -> list[dict]:
    return [
        _qa("w-insert-option", "dynamodb", OPTIONS),
        _qa("w-update-option", "dynamodb", OPTIONS),
        _qa("w-delete-postmeta", "dynamodb", POSTMETA),
        _qa("r-option-by-name", "dynamodb", OPTIONS),
        _qa("r-post-with-meta", "aurora_mysql", POSTS, POSTMETA),
        _qa("r-recent-posts", "aurora_mysql", POSTS),
        _qa("r-autoload-options", "aurora_mysql", OPTIONS),
        _qa("r-post-archive-count", "aurora_mysql", POSTS),
        _qa("r-post-by-slug", "aurora_mysql", POSTS),
    ]


WRITE_GROUPS = {OPTIONS: ["w-insert-option", "w-update-option"], POSTMETA: ["w-delete-postmeta"]}

# What the model must be told about the fixture.
DYNAMODB_OWNS_EVERY_WRITE = {
    "write_engines": {"dynamodb": 3},
    "read_only_engines": ["aurora_mysql"],
    "tables": [
        {
            "table": OPTIONS,
            "source_of_truth": "dynamodb",
            "write_engines": ["dynamodb"],
            "replica_engines": ["aurora_mysql"],
        },
        {
            "table": POSTMETA,
            "source_of_truth": "dynamodb",
            "write_engines": ["dynamodb"],
            "replica_engines": ["aurora_mysql"],
        },
    ],
}

# The two summaries the approval gate showed (wordpress-bundled, then wordpress-live).
BUNDLED_SUMMARY = (
    "The analysis covered 50 source tables and 107 query patterns across Amazon Aurora "
    "MySQL, Amazon DocumentDB, Amazon DynamoDB and Amazon OpenSearch Service. Amazon "
    "DocumentDB and Amazon OpenSearch Service were eliminated: their joined action-scheduler "
    "and user-meta reads, and the one date-bucketed post count, are relational queries "
    "Aurora MySQL serves with joins and GROUP BY, and the option, user-meta and API-key "
    "reads are key lookups DynamoDB serves natively through its primary key or a GSI. Aurora "
    "MySQL carries the transactional writes and the multi-table queries, while DynamoDB "
    "serves the high-volume key lookups from a copy kept in sync through change data capture."
)
LIVE_SUMMARY = (
    "The analysis covered 50 source tables and 165 query patterns across Amazon Aurora "
    "MySQL, Amazon DocumentDB, Amazon DynamoDB and Amazon OpenSearch Service. Amazon "
    "DocumentDB and Amazon OpenSearch Service are eliminated: the user, comment and action "
    "scheduler queries they held are relational joins, LIKE filters and keyed lookups that "
    "Aurora MySQL serves with joins and secondary indexes, the comment metadata and user "
    "lookups are key and GSI reads DynamoDB serves natively, and the single post archive "
    "count is a date-range GROUP BY Aurora MySQL runs directly. Aurora MySQL carries the "
    "writes and the relational reads, DynamoDB serves the key-value reads from a copy kept "
    "in sync through change data capture."
)
CORRECT_SUMMARY = (
    "The analysis covered 3 source tables and 9 query patterns across Amazon Aurora MySQL, "
    "Amazon DocumentDB and Amazon DynamoDB, and Amazon DocumentDB is consolidated into "
    "DynamoDB. DynamoDB carries every write and the key lookups, while Aurora MySQL serves "
    "the multi-table reads from a copy kept in sync from DynamoDB through change data capture."
)


def _det(write_groups: dict | None = WRITE_GROUPS, qas: list[dict] | None = None) -> dict:
    qas = _qas() if qas is None else qas
    assignment: dict = {"version": 1, "query_assignments": deepcopy(qas)}
    if write_groups is not None:
        assignment["write_groups"] = write_groups
    after: dict[str, int] = {}
    for qa in qas:
        after[qa["assigned_engine"]] = after.get(qa["assigned_engine"], 0) + 1
    return {
        "assignment": assignment,
        "revised_assignments": deepcopy(qas),
        "collector_output": {"queries": {"query_patterns": []}},
        "triage": {},
        "consolidations": [],
        "before_distribution": dict(after),
        "after_distribution": after,
        "unique_value_assessment": {},
        "architectural_patterns": patterns_for_assignment(qas, write_groups),
        "recommendations": [],
    }


def _ownership(write_groups: dict | None = WRITE_GROUPS, qas: list[dict] | None = None):
    qas = _qas() if qas is None else qas
    assignment: dict = {"query_assignments": qas}
    if write_groups is not None:
        assignment["write_groups"] = write_groups
    return write_ownership(assignment, qas)


class TestTheModelIsToldWhoCarriesTheWrites:
    def test_llm_input_carries_the_write_owners(self):
        es = prepare_reality_check_llm_input(_det())["executive_summary"]
        assert es["write_ownership"] == DYNAMODB_OWNS_EVERY_WRITE

    def test_the_owners_follow_the_revised_assignments_not_the_input(self):
        det = _det()
        # The input version had the option writes on Aurora MySQL; the check moved them.
        for qa in det["assignment"]["query_assignments"]:
            if qa["query_id"].startswith("w-"):
                qa["assigned_engine"] = "aurora_mysql"
        es = prepare_reality_check_llm_input(det)["executive_summary"]
        assert es["write_ownership"]["write_engines"] == {"dynamodb": 3}

    def test_no_writes_at_all(self):
        es = prepare_reality_check_llm_input(_det({}))["executive_summary"]
        assert es["write_ownership"] == {
            "write_engines": {},
            "read_only_engines": ["aurora_mysql", "dynamodb"],
            "tables": [],
        }

    def test_writes_split_across_two_engines(self):
        qas = _qas() + [_qa("w-insert-post", "aurora_mysql", POSTS)]
        groups = {**WRITE_GROUPS, POSTS: ["w-insert-post"]}
        ownership = prepare_reality_check_llm_input(_det(groups, qas))["executive_summary"][
            "write_ownership"
        ]
        assert ownership["write_engines"] == {"aurora_mysql": 1, "dynamodb": 3}
        assert ownership["read_only_engines"] == []
        posts = next(t for t in ownership["tables"] if t["table"] == POSTS)
        assert posts["source_of_truth"] == "aurora_mysql"
        assert posts["replica_engines"] == []

    def test_one_table_written_on_two_engines_has_no_source_of_truth(self):
        qas = _qas()
        qas[1]["assigned_engine"] = "aurora_mysql"  # w-update-option
        ownership = _ownership(qas=qas)
        options = next(t for t in ownership["tables"] if t["table"] == OPTIONS)
        assert options["source_of_truth"] is None
        assert options["write_engines"] == ["aurora_mysql", "dynamodb"]
        assert options["replica_engines"] == []

    def test_an_assignment_without_write_groups_adds_nothing(self):
        es = prepare_reality_check_llm_input(_det(None))["executive_summary"]
        assert "write_ownership" not in es


class TestPatternsNameTheWriteOwner:
    """The patterns in the model's input named the busiest engine as the writer."""

    def test_no_cqrs_around_an_engine_without_writes(self):
        patterns = patterns_for_assignment(_qas(), WRITE_GROUPS)
        assert not [p for p in patterns if "write_engine" in p["applies_to"]]

    def test_the_sync_runs_from_the_write_owner(self):
        (sync,) = [
            p
            for p in patterns_for_assignment(_qas(), WRITE_GROUPS)
            if "source_engine" in p["applies_to"]
        ]
        assert sync["applies_to"] == {
            "source_engine": "dynamodb",
            "target_engines": ["aurora_mysql"],
        }
        assert sync["example"].startswith("Change data capture on DynamoDB carries")

    def test_the_sync_keeps_the_busiest_engine_when_writes_split(self):
        qas = _qas() + [_qa("w-insert-post", "aurora_mysql", POSTS)]
        (sync,) = [
            p
            for p in patterns_for_assignment(qas, {**WRITE_GROUPS, POSTS: ["w-insert-post"]})
            if "source_engine" in p["applies_to"]
        ]
        assert sync["applies_to"]["source_engine"] == "aurora_mysql"

    def test_a_search_view_is_fed_from_the_write_owner(self):
        qas = _qas() + [_qa("r-post-search", "opensearch", POSTS)]
        (view,) = [
            p
            for p in _detect_architectural_patterns(qas, set(), {}, {"dynamodb"})
            if "view_engine" in p["applies_to"]
        ]
        assert view["applies_to"] == {"source_engine": "dynamodb", "view_engine": "opensearch"}

    @pytest.mark.passes_on_main
    def test_without_write_groups_the_detection_is_unchanged(self):
        (sync,) = [
            p
            for p in _detect_architectural_patterns(_qas(), set(), {}, None)
            if "source_engine" in p["applies_to"]
        ]
        assert sync["applies_to"] == {
            "source_engine": "aurora_mysql",
            "target_engines": ["dynamodb"],
        }


class TestTheSummaryIsCheckedAgainstTheWriteOwners:
    @pytest.mark.parametrize("summary", [BUNDLED_SUMMARY, LIVE_SUMMARY], ids=["bundled", "live"])
    def test_the_gate_summaries_are_rejected(self, summary):
        findings = check_summary_write_claims(summary, _ownership())
        assert any("Aurora MySQL as carrying writes" in f for f in findings), findings
        assert any("DynamoDB as reading a synced copy" in f for f in findings), findings
        assert all("DynamoDB (3)" in f for f in findings)

    @pytest.mark.parametrize(
        "summary",
        [
            CORRECT_SUMMARY,
            "Change data capture on DynamoDB carries each committed change to Aurora MySQL.",
            "DynamoDB takes all the writes. Aurora MySQL reads a replica of the option and "
            "post metadata tables, fed by DynamoDB Streams.",
            "Aurora MySQL takes no writes: it serves the joined post reads.",
            "Aurora MySQL serves the relational reads and DynamoDB the key lookups.",
        ],
    )
    def test_a_correct_summary_passes(self, summary):
        assert check_summary_write_claims(summary, _ownership()) == []

    @pytest.mark.parametrize(
        "summary",
        [
            "DynamoDB takes no writes.",
            "Aurora MySQL takes every write.",
            "Aurora MySQL serves its reads from a copy kept in sync from Aurora MySQL.",
            "Change data capture on Aurora MySQL keeps DynamoDB in sync.",
        ],
    )
    def test_a_reversed_or_impossible_claim_is_rejected(self, summary):
        assert check_summary_write_claims(summary, _ownership())

    def test_a_writer_named_at_the_end_of_a_list_is_caught(self):
        # Another WordPress run: the writes close a list after the engine.
        summary = (
            "Aurora MySQL carries the joins, aggregations and transactional writes, while "
            "DynamoDB serves the high-frequency key lookups."
        )
        assert check_summary_write_claims(summary, _ownership()) == [
            "The summary describes Aurora MySQL as carrying writes, but the final assignment "
            "puts none on it: the writes sit on DynamoDB (3)."
        ]

    def test_no_writes_at_all(self):
        ownership = _ownership({})
        assert check_summary_write_claims("Aurora MySQL carries the writes.", ownership) == [
            "The summary describes Aurora MySQL as carrying writes, but the final assignment "
            "puts none on it: no engine carries a write."
        ]
        assert (
            check_summary_write_claims(
                "Aurora MySQL serves the relational reads and DynamoDB the key lookups.", ownership
            )
            == []
        )

    def test_writes_split_across_two_engines(self):
        qas = _qas() + [_qa("w-insert-post", "aurora_mysql", POSTS)]
        ownership = _ownership({**WRITE_GROUPS, POSTS: ["w-insert-post"]}, qas)
        split = "Aurora MySQL carries the post writes and DynamoDB the option writes."
        assert check_summary_write_claims(split, ownership) == []
        assert check_summary_write_claims("DynamoDB takes every write.", ownership) == [
            "The summary describes DynamoDB as taking every write, but the writes sit on "
            "DynamoDB (3) and Aurora MySQL (1)."
        ]

    def test_a_cache_reads_a_copy_and_carries_no_writes(self):
        # ElastiCache is an overlay: no query is assigned to it, it caches the reads.
        ownership = _ownership()
        cached = (
            "ElastiCache serves the hot option reads from a cached copy kept in sync from DynamoDB."
        )
        assert check_summary_write_claims(cached, ownership) == []
        assert check_summary_write_claims("ElastiCache carries the option writes.", ownership)

    @pytest.mark.passes_on_main
    def test_an_assignment_without_write_groups_is_not_checked(self):
        assert check_summary_write_claims(BUNDLED_SUMMARY, None) == []


class TestFinalizeFallsBackOnAReversedWriteOwner:
    def _result(self, summary: str) -> dict:
        det = _det()
        det["executive_summary"] = summary
        return det

    def test_the_bundled_summary_is_replaced_and_kept_for_audit(self):
        result = self._result(BUNDLED_SUMMARY)
        finalize_executive_summary(result)
        assert result["executive_summary_source"] == "deterministic_fallback"
        assert result["executive_summary_llm"] == BUNDLED_SUMMARY
        assert any(
            "Aurora MySQL as carrying writes" in w
            for w in result["executive_summary_validation_warnings"]
        )
        assert "carries the transactional writes" not in result["executive_summary"]

    def test_a_correct_summary_is_kept(self):
        summary = (
            "DynamoDB carries every write and the key lookups, while Aurora MySQL serves the "
            "multi-table reads from a copy kept in sync from DynamoDB through change data capture."
        )
        result = self._result(summary)
        finalize_executive_summary(result)
        assert result["executive_summary_validation_warnings"] == []
        assert result["executive_summary_source"] == "llm"
        assert result["executive_summary"] == summary
