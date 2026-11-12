# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What every job's engine choices must hold, before and after ``--decide-for-me``
(ADR-031 Decision 12 Amendment 10). Shared by the discourse choices test and the DNN
placeholder-table test (#2147), whose ties include statements that read no table."""

from __future__ import annotations


def assert_every_open_tie_is_one_choice(report: dict) -> None:
    """ADR-031 Amendment 10: every open tie sits in exactly one choice, so the counts the
    report states agree (a statement that reads no table included, #2147)."""
    listed = [q for c in report["pending_engine_choices"] for q in c["query_ids"]]
    tied = [t["query_id"] for t in report["tied_queries"]]
    assert sorted(listed) == sorted(tied) and len(listed) == len(set(listed))
    facts = report["facts"]
    assert facts["pending_choice_query_count"] == facts["tied_query_count"] == len(tied)


def assert_decide_for_me_leaves_nothing_open(assignment: dict, report: dict) -> None:
    """After ``--decide-for-me``: no query keeps a tie, no choice is pending, and no
    "until you choose" tie risk is left that the customer has nothing to act on (#2147)."""
    assert not [qa["query_id"] for qa in assignment["query_assignments"] if qa["tied_engines"]]
    assert report["pending_engine_choices"] == [] and report["tied_queries"] == []
    assert report["facts"]["tied_query_count"] == 0
    risks = report["risk_assessment"]["risks"]
    assert not [r["risk_id"] for r in risks if r.get("rule") == "routing.tie_open"]
