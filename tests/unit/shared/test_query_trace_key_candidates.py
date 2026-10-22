"""The decision trace carries the keys the redesign considered (#1361).

``build_query_trace`` takes a query's ``candidate_keys`` rows from the assignment's
access-pattern record; synthesis passes them into ``report.json``'s ``query_traces``
and the context graph into the trace API, the engineering report's appendix and the
UI's "Why this engine" show them. A trace or record written before #1361 has none and
still loads. No test calls a model.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from src.contracts.query_trace import QueryTraceDigest, QueryTraceEntry, QueryTraceResponse
from src.report.query_traces import query_traces_md
from src.shared.key_refusals import candidate_explanation
from src.shared.query_trace import (
    build_query_trace,
    candidate_keys_by_query,
    key_candidates_of,
    query_trace_digest,
)
from src.shared.report_facts import compute_report_facts

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "wordpress_move_trace.json"

# A row as the access-pattern record stores it: codes only (#1361 review).
REFUSED_REASONS = ["already_a_key", "rewritten_by_write", "no_selectivity_signal"]
REFUSED_ROW: dict = {
    "column": "user_profiles.granted_title_badge_id",
    "table_id": "discourse.user_profiles",
    "via": "not_null_test",
    "outcome": "refused",
    "reasons": REFUSED_REASONS,
}
# The same row as the trace shows it: the sentence built from the codes when read.
SHOWN_ROW = {
    **REFUSED_ROW,
    "explanation": candidate_explanation("not_null_test", REFUSED_REASONS),
}


def _sample() -> dict:
    data: dict = json.loads(FIXTURE.read_text())
    return data


def test_a_trace_carries_the_rows_it_is_given_with_the_sentence_built_from_the_codes():
    trace = build_query_trace("q", [], "dynamodb", {"dynamodb"}, 2, [REFUSED_ROW])
    (key,) = trace.key_candidates
    assert key.model_dump() == SHOWN_ROW
    assert key.explanation == (
        "refused for an IS NOT NULL test (a sparse index): 3 reasons: (1) already indexed in "
        "the source, which an IS NOT NULL test does not anchor on as written, and the "
        "redesign adds no key a column already has; (2) a column the statement's own write "
        "rewrites; (3) an IS NOT NULL test with no selectivity signal (no partial index or "
        "statistics), which may read most rows"
    )


def test_a_stored_sentence_is_rebuilt_from_the_codes():
    """A row written while the record still stored its sentence (or a hand-edited one)
    shows the sentence the codes give, so the trace and the evidence never disagree."""
    stale = {**REFUSED_ROW, "explanation": "an older wording"}
    (key,) = key_candidates_of([stale])
    assert key.explanation == SHOWN_ROW["explanation"]


def test_a_trace_written_before_1361_loads_with_none():
    trace = build_query_trace("q", [], "dynamodb", {"dynamodb"}, 2)
    legacy = trace.model_dump(mode="json", by_alias=True)
    legacy.pop("key_candidates")
    assert QueryTraceResponse.model_validate(legacy).key_candidates == []
    entry = QueryTraceEntry.model_validate({**legacy, "source_tables": ["t"]})
    assert entry.key_candidates == []
    digest = QueryTraceDigest.model_validate(
        {"has_ledger": False, "query_count": 1, "queries": [{**legacy, "source_tables": []}]}
    )
    assert digest.queries[0].key_candidates == []


def test_a_row_this_release_cannot_read_is_left_out():
    newer = {**REFUSED_ROW, "reasons": ["a_guard_from_a_later_release"]}
    assert key_candidates_of([newer, REFUSED_ROW, "junk"]) == key_candidates_of([REFUSED_ROW])


def test_a_record_before_version_3_holds_none():
    legacy = {"schema_version": 2, "queries": [{"query_id": "q", "redesign": None}]}
    assert candidate_keys_by_query(legacy) == {}
    assert candidate_keys_by_query(None) == {}
    record = {"schema_version": 3, "queries": [{"query_id": "q", "candidate_keys": [REFUSED_ROW]}]}
    assert candidate_keys_by_query(record) == {"q": [REFUSED_ROW]}


def _listed_digest(keys_for: dict[str, list[dict]]) -> dict:
    sample = _sample()
    versions = {1: sample["v1"], 2: sample["v2"]}
    digest = query_trace_digest(sample["v2"], versions.get, 2, key_candidates=keys_for)
    return digest.model_dump(mode="json", by_alias=True)


def test_the_digest_passes_each_querys_rows_and_the_appendix_shows_them():
    first = _listed_digest({})["queries"][0]["query_id"]
    digest = _listed_digest({first: [REFUSED_ROW]})
    by_id = {t["query_id"]: t for t in digest["queries"]}
    assert by_id[first]["key_candidates"][0]["reasons"] == REFUSED_ROW["reasons"]
    assert all(not t["key_candidates"] for q, t in by_id.items() if q != first)
    report = {"query_traces": digest, "ranking": [], "schema_designs": {}}
    report["facts"] = compute_report_facts(report)
    md = "\n".join(query_traces_md(report))
    assert "- Redesign key `user_profiles.granted_title_badge_id`: refused for" in md
    assert "a column the statement's own write rewrites" in md


def test_queries_with_other_keys_are_not_folded_into_one_group():
    """The appendix folds queries whose traces read the same (#1108); a key the trace
    names is the query's own, so two queries that differ only there are two entries."""
    from src.shared.report_facts import _trace_signature

    trace = build_query_trace("q", [], "dynamodb", {"dynamodb"}, 2, [REFUSED_ROW])
    plain = trace.model_dump(mode="json", by_alias=True)
    other = copy.deepcopy(plain)
    other["key_candidates"][0]["column"] = "user_profiles.other_column"
    assert _trace_signature(plain, {}) != _trace_signature(other, {})
    assert _trace_signature(plain, {}) == _trace_signature(copy.deepcopy(plain), {})


def test_synthesis_reads_the_effective_versions_record(tmp_path):
    """report.json's query_traces carry the effective version's candidate keys, read
    from ``assignment/v<N>/access_patterns.json``; a job with no record gets none."""
    from src.agents.referee.synthesis_handler import _query_traces
    from src.storage.assignment_versioning import assignment_artifact_path
    from src.storage.local_store import LocalArtifactStore

    sample = _sample()
    store = LocalArtifactStore(str(tmp_path))
    for version in (1, 2):
        store.write_json(assignment_artifact_path("wp", "j", version), sample[f"v{version}"])
    before = _query_traces(store, "wp", "j", sample["v2"], 2)
    first = before["queries"][0]["query_id"]
    assert all(not t["key_candidates"] for t in before["queries"])
    store.write_json(
        "wp/j/assignment/v2/access_patterns.json",
        {"schema_version": 3, "queries": [{"query_id": first, "candidate_keys": [REFUSED_ROW]}]},
    )
    after = _query_traces(store, "wp", "j", sample["v2"], 2)
    by_id = {t["query_id"]: t for t in after["queries"]}
    assert by_id[first]["key_candidates"] == [SHOWN_ROW]
