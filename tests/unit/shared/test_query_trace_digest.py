"""The per-query decision traces synthesis writes into report.json (#339).

``src.shared.query_trace.query_trace_digest`` traces every query of the
effective assignment from the move ledger of its lineage, keeps in full only
the queries whose routing has a story (moved after the first assignment, ever
left tied, overridden) and counts the rest by their first assignment's rule.
The fixture is the wordpress sample's real v1/v2 ledger, trimmed
(``tests/fixtures/wordpress_move_trace.json``).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from src.contracts.query_trace import QueryTraceDigest
from src.shared import query_trace as query_trace_module
from src.shared.query_trace import (
    assignment_lineage,
    build_query_trace,
    ledger_moves,
    query_trace_digest,
)
from src.shared.report_facts import (
    TRACE_FULL_CAP,
    is_listed,
    query_trace_facts,
    trace_kinds,
    trace_order,
)

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "wordpress_move_trace.json"


@pytest.fixture
def sample() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text())
    return data


def _digest(sample: dict[str, Any], effective: str = "v2") -> QueryTraceDigest:
    versions = {1: sample["v1"], 2: sample["v2"]}
    for key in ("v3", "v4"):
        if key in sample:
            versions[sample[key]["version"]] = sample[key]
    eff = sample[effective]
    return query_trace_digest(eff, versions.get, eff["version"])


def _moved_qids(sample: dict[str, Any]) -> set[str]:
    return {
        m["query_id"]
        for m in sample["v2"]["moves"]
        if m.get("from_engine") and m["from_engine"] != m["to_engine"]
    }


def test_lists_the_moved_queries_in_full_and_counts_the_rest_by_rule(sample) -> None:
    digest = _digest(sample)
    assert digest.has_ledger
    assert digest.query_count == len(sample["v2"]["query_assignments"])
    listed = {t.query_id for t in digest.queries}
    assert _moved_qids(sample) <= listed
    # Every query is either listed in full or counted under one rule.
    assert len(listed) + sum(r.query_count for r in digest.unlisted_rules) == digest.query_count
    kinds = [trace_kinds(t.model_dump(mode="json", by_alias=True)) for t in digest.queries]
    rows = [{"query_id": t.query_id, **k} for t, k in zip(digest.queries, kinds, strict=True)]
    assert rows == sorted(rows, key=trace_order)  # most notable first
    assert all(is_listed(k) for k in kinds)
    assert digest.more_listed == []  # well under the cap
    for rule in digest.unlisted_rules:
        assert rule.label and rule.label != rule.rule.value


def test_a_listed_trace_carries_every_step_with_its_reason(sample) -> None:
    replace_into = next(
        m["query_id"]
        for m in sample["v2"]["moves"]
        if m["reason_code"] == "consolidate.no_unique_value" and m["to_engine"] == "dynamodb"
    )
    trace = next(t for t in _digest(sample).queries if t.query_id == replace_into)
    assert trace.first_assignment is not None and trace.first_assignment.version == 1
    assert [s.version for s in trace.moves][-1] == 2
    assert trace.final_engine == "dynamodb" and trace.final_version == 2
    assert trace.effective_version == 2
    assert trace.runner_up is not None and trace.runner_up.gap is not None
    assert trace.runner_up.gap >= 0
    assert all(s.explanation for s in [trace.first_assignment, *trace.moves])


def test_the_lineage_skips_an_abandoned_version(sample) -> None:
    # v3 is a fresh resolution (no previous_version): v1 and v2 are not its history.
    v3 = copy.deepcopy(sample["v1"])
    v3["version"] = 3
    v3["previous_version"] = None
    sample["v3"] = v3
    digest = _digest(sample, "v3")
    assert digest.effective_version == 3
    for trace in digest.queries:
        assert {s.version for s in [trace.first_assignment, *trace.moves] if s} == {3}
    lineage = assignment_lineage({1: sample["v1"], 2: sample["v2"], 3: v3}.get, 3)
    assert [a["version"] for a in lineage] == [3]
    assert set(ledger_moves(lineage)) <= {qa["query_id"] for qa in v3["query_assignments"]}


def test_an_assignment_written_before_the_ledger_has_no_traces(sample) -> None:
    for key in ("v1", "v2"):
        sample[key].pop("moves", None)
    digest = _digest(sample)
    assert not digest.has_ledger
    assert digest.queries == [] and digest.unlisted_rules == []
    facts = query_trace_facts({"query_traces": digest.model_dump(mode="json", by_alias=True)})
    assert facts is not None and facts["has_ledger"] is False
    assert facts["listed_query_count"] == 0
    assert facts["unlisted_query_count"] == digest.query_count


def test_untrusted_artifact_text_never_reaches_the_digest(sample) -> None:
    planted = "<script>alert(1)</script> [x](http://evil)"
    for key in ("v1", "v2"):
        for qa in sample[key]["query_assignments"]:
            qa["assignment_reason"] = planted
            qa["explanation"] = planted
    dumped = json.dumps(_digest(sample).model_dump(mode="json", by_alias=True))
    assert "script" not in dumped and "evil" not in dumped


def test_trace_kinds() -> None:
    first: dict[str, Any] = {
        "from_engine": None,
        "to_engine": "dynamodb",
        "actor": "rule",
        "tied_engines": [],
    }
    same = {"from_engine": "dynamodb", "to_engine": "dynamodb", "actor": "rule"}
    moved = {"from_engine": "dynamodb", "to_engine": "aurora_mysql", "actor": "rule"}
    assert trace_kinds({"first_assignment": first, "moves": [same]}) == {
        "moved": False,
        "tied": False,
        "open_tie": False,
        "overridden": False,
        "pinned": False,
    }
    assert trace_kinds({"first_assignment": first, "moves": [moved]})["moved"]
    # Moved and moved back is still a move.
    back = {"from_engine": "aurora_mysql", "to_engine": "dynamodb", "actor": "rule"}
    assert trace_kinds({"first_assignment": first, "moves": [moved, back]})["moved"]
    tied_once = {**first, "tied_engines": ["dynamodb", "documentdb"]}
    settled = trace_kinds({"first_assignment": tied_once, "moves": [same]})
    assert settled["tied"] and not settled["open_tie"]
    still = trace_kinds({"first_assignment": first, "tied_engines": ["dynamodb", "x"]})
    assert still["tied"] and still["open_tie"]
    customer = {**moved, "actor": "customer"}
    assert trace_kinds({"first_assignment": first, "moves": [customer]})["overridden"]
    flag = {**moved, "actor": "model_flag"}
    assert trace_kinds({"first_assignment": first, "moves": [flag]})["overridden"]
    # A listed rule pins a query; it is not an outside override (#339 review).
    search = {**first, "to_engine": "opensearch", "reason_code": "override.text_search"}
    pinned = trace_kinds({"first_assignment": search, "moves": []})
    assert pinned["pinned"] and not pinned["overridden"]
    utility = {**first, "to_engine": "aurora_mysql", "reason_code": "override.utility"}
    assert not is_listed(trace_kinds({"first_assignment": utility, "moves": []}))


def test_steps_are_numbered_per_query_and_name_the_deciding_step(sample) -> None:
    """#339 review: "step N" is the query's own step, never the ledger's
    version-wide seq."""
    for trace in _digest(sample).queries:
        steps = [s for s in [trace.first_assignment, *trace.moves] if s is not None]
        assert [s.step for s in steps] == list(range(1, len(steps) + 1))
        for better in trace.outscored_by:
            step = steps[better.decided_by_step - 1]
            assert (step.version, step.seq) == (better.decided_by_version, better.decided_by_seq)


def test_a_group_placed_query_carries_its_group_mean(sample) -> None:
    """#339 review: the group's mean on the final engine is its own field, next
    to the query's own final_score."""
    v1 = sample["v1"]
    move = next(m for m in v1["moves"] if m["reason_code"] == "group.mean")
    moves = ledger_moves([v1])[move["query_id"]]
    trace = build_query_trace(move["query_id"], moves, move["to_engine"], {move["to_engine"]})
    mean = move["evidence"]["group_scores"][move["to_engine"]]
    assert trace.final_group_mean == round(mean, 1)
    assert trace.final_score == move["score_after"]


def test_the_listing_carries_the_tables_and_an_excerpt_of_the_sql(sample) -> None:
    versions = {1: sample["v1"], 2: sample["v2"]}
    long_sql = "REPLACE   INTO wp_wc_product_meta_lookup\n (product_id, sku) " + "x" * 300
    texts = {qa["query_id"]: long_sql for qa in sample["v2"]["query_assignments"]}
    digest = query_trace_digest(sample["v2"], versions.get, 2, texts)
    entry = digest.queries[0]
    qa = next(q for q in sample["v2"]["query_assignments"] if q["query_id"] == entry.query_id)
    assert entry.source_tables == qa["source_tables"]
    assert entry.query_excerpt is not None
    assert entry.query_excerpt.startswith("REPLACE INTO wp_wc_product_meta_lookup (product_id")
    assert len(entry.query_excerpt) == query_trace_module.EXCERPT_CHARS
    assert entry.query_excerpt.endswith("\u2026")


def test_only_the_cap_carry_their_full_trace(sample, monkeypatch) -> None:
    """#339 review: at most TRACE_FULL_CAP full traces; the rest say only why."""
    full = _digest(sample)
    monkeypatch.setattr(query_trace_module, "TRACE_FULL_CAP", 2)
    capped = _digest(sample)
    assert [t.query_id for t in capped.queries] == [t.query_id for t in full.queries[:2]]
    assert [m.query_id for m in capped.more_listed] == [t.query_id for t in full.queries[2:]]
    facts = query_trace_facts({"query_traces": capped.model_dump(mode="json", by_alias=True)})
    assert facts is not None
    assert (facts["listed_query_count"], facts["full_trace_count"]) == (len(full.queries), 2)
    assert facts["not_in_full_count"] == len(full.queries) - 2
    uncapped = query_trace_facts({"query_traces": full.model_dump(mode="json", by_alias=True)})
    assert uncapped is not None
    # The cap is the facts layer's, the one synthesis applies.
    assert uncapped["full_trace_cap"] == TRACE_FULL_CAP == 200
    assert query_trace_module.TRACE_FULL_CAP == 2  # (patched above)
    for key in ("moved_query_count", "ever_tied_query_count", "open_tie_query_count"):
        assert facts[key] == uncapped[key], key
