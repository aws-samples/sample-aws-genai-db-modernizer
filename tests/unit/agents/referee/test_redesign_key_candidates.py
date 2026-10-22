"""The redesign trace names why each candidate key was refused (#1361, follow-up 1 of #1359).

ADR-031 Decision 2 Amendment 13 reads each query after redesign: a column it filters on
may get the key the access needs, unless a guard refuses it. Before #1361 a refusal by
some guards left no trace (a column the source already indexes was skipped before any
check, a correlated pin or a keyset walk's ``IS NOT NULL`` was dropped), and a column
refused by several guards was named with the first only. Now every column the redesign
considers is recorded, accepted or refused with every guard that refused it, in the
anti-pattern evidence, the access-pattern record and the decision trace. No scoring
changes: what the redesign accepts is what it accepted before. No test calls a model.
"""

from __future__ import annotations

import json
import typing
import zipfile
from pathlib import Path

import pytest

from src.agents.referee.query_pattern import QueryPatternModel, access_patterns_record
from src.agents.referee.redesign_findings import (
    ACCEPTED,
    ALREADY_A_KEY,
    COMPOSITE_TAIL,
    CORRELATED_PIN,
    INTRINSIC,
    KEYSET_WALK,
    NO_SELECTIVITY_SIGNAL,
    NOT_KEY_ELIGIBLE,
    REFUSAL_REASONS,
    REFUSED,
    REWRITTEN_BY_WRITE,
    VIA_EQUALITY,
    VIA_NOT_NULL_TEST,
    VIA_OR_BRANCH,
    RedesignModel,
)
from src.contracts.query_trace import KeyRefusal
from src.shared.key_refusals import candidate_explanation, refusal_sentence
from src.shared.query_trace import key_candidates_of

REPO = Path(__file__).resolve().parents[4]
SWEEP = "2539559289466738950"  # discourse: the title-badge reconciliation sweep (#1359)
WIDE_WRITE = "wide-transaction"


@pytest.fixture(scope="module")
def discourse() -> dict:
    """The bundled discourse sample, ingested as the collect step does."""
    from src.agents.collector.offline_ingest import build_offline_collector_output
    from src.tools.database.offline_parser import parse_offline_collection

    with zipfile.ZipFile(REPO / "docs/examples/discourse/discourse.zip") as z:
        raw = json.loads(z.read("discourse-collection.json"))
    return build_offline_collector_output(
        parse_offline_collection(raw), job_id="t", database_name="discourse", s3_key="t"
    )


def _query(collector: dict, qid: str) -> dict:
    return next(q for q in collector["queries"]["query_patterns"] if str(q["query_id"]) == qid)


class TestTheDiscourseSweep:
    """``UPDATE user_profiles up SET granted_title_badge_id = $1 FROM users u WHERE ...
    AND up.granted_title_badge_id IS NOT NULL``: three guards each refuse its only
    candidate key, and the trace names all three."""

    def test_its_one_candidate_is_refused_by_all_three_guards(self, discourse):
        model = RedesignModel.from_collector(discourse)
        (candidate,) = model.candidate_keys(_query(discourse, SWEEP))
        assert (candidate.label, candidate.via, candidate.outcome) == (
            "user_profiles.granted_title_badge_id",
            VIA_NOT_NULL_TEST,
            REFUSED,
        )
        assert candidate.reasons == (ALREADY_A_KEY, REWRITTEN_BY_WRITE, NO_SELECTIVITY_SIGNAL)
        text = candidate.explanation()
        assert ": 3 reasons: " in text
        for reason in candidate.reasons:
            assert REFUSAL_REASONS[reason] in text

    def test_the_wide_transaction_evidence_names_the_column_and_every_guard(self, discourse):
        model = RedesignModel.from_collector(discourse)
        label = model.classify(_query(discourse, SWEEP), WIDE_WRITE)
        assert label.kind == INTRINSIC
        assert "user_profiles.granted_title_badge_id: 3 reasons" in label.evidence
        for reason in (ALREADY_A_KEY, REWRITTEN_BY_WRITE, NO_SELECTIVITY_SIGNAL):
            assert REFUSAL_REASONS[reason] in label.evidence

    def test_the_record_lists_the_refusal_and_routing_still_has_no_redesign(self, discourse):
        record = access_patterns_record(discourse, {})
        assert record["schema_version"] == 3
        row = next(r for r in record["queries"] if r["query_id"] == SWEEP)
        assert row["redesign"] is None
        (entry,) = row["candidate_keys"]
        assert entry["column"] == "user_profiles.granted_title_badge_id"
        assert entry["table_id"].endswith("user_profiles")
        assert entry["outcome"] == REFUSED and entry["via"] == VIA_NOT_NULL_TEST
        assert entry["reasons"] == [ALREADY_A_KEY, REWRITTEN_BY_WRITE, NO_SELECTIVITY_SIGNAL]
        # The record stores codes only; the trace builds the sentence when it reads them.
        assert "explanation" not in entry
        (shown,) = key_candidates_of([entry])
        assert shown.reasons == entry["reasons"]
        assert shown.explanation == candidate_explanation(entry["via"], entry["reasons"])


# ---------------------------------------------------------------------------
# Every guard, on a small schema
# ---------------------------------------------------------------------------


def _column(name: str, data_type: str) -> dict:
    return {"column_name": name, "data_type": data_type}


def _collector(text: str, qtype: str = "SELECT") -> tuple[dict, dict]:
    query = {
        "query_id": "q",
        "query_text": text,
        "query_type": qtype,
        "calls_per_second": 5.0,
        "tables_accessed": ["shop.orders"],
    }
    collector = {
        "metadata": {"source_database": {"engine": "postgresql"}},
        "database_schema": {
            "tables": [
                {
                    "table_id": "shop.orders",
                    "table_name": "orders",
                    "primary_key": ["id"],
                    "row_count": 100_000,
                    "columns": [
                        _column("id", "integer"),
                        _column("customer_id", "integer"),
                        _column("email", "varchar(20)"),
                        _column("token", "varchar(64)"),
                        _column("prev_token", "varchar(64)"),
                        _column("line_no", "integer"),
                        _column("status", "varchar(20)"),
                        _column("claimed_by", "integer"),
                        _column("archived_by", "integer"),
                    ],
                    "indexes": [
                        {"index_name": "o_cust", "columns": ["customer_id"]},
                        {
                            "index_name": "o_line",
                            "columns": ["customer_id", "line_no"],
                            "is_unique": True,
                        },
                        {"index_name": "o_arch", "columns": ["archived_by"]},
                        {
                            "index_name": "o_open",
                            "columns": ["id"],
                            "predicate": "claimed_by IS NOT NULL",
                        },
                    ],
                },
                {
                    "table_id": "shop.customers",
                    "table_name": "customers",
                    "primary_key": ["id"],
                    "row_count": 20_000,
                    "columns": [_column("id", "integer"), _column("email", "varchar(20)")],
                    "indexes": [],
                },
            ]
        },
        "queries": {"query_patterns": [query]},
    }
    return collector, query


def _candidates(text: str, qtype: str = "SELECT") -> list[tuple[str, str, str, tuple[str, ...]]]:
    collector, query = _collector(text, qtype)
    model = RedesignModel.from_collector(collector)
    return [(c.label, c.via, c.outcome, c.reasons) for c in model.candidate_keys(query)]


class TestEachGuardIsNamed:
    def test_an_accepted_key_is_recorded_as_accepted(self):
        assert _candidates("SELECT * FROM orders WHERE email = $1") == [
            ("orders.email", VIA_EQUALITY, ACCEPTED, ())
        ]

    def test_each_or_branch_is_its_own_candidate(self):
        assert _candidates("SELECT * FROM orders WHERE token = $1 OR prev_token = $2") == [
            ("orders.prev_token", VIA_OR_BRANCH, ACCEPTED, ()),
            ("orders.token", VIA_OR_BRANCH, ACCEPTED, ()),
        ]

    @pytest.mark.passes_on_main  # main never keys a pin on a source key either
    def test_a_pin_on_a_source_key_is_anchored_as_written_and_no_candidate(self):
        assert _candidates("SELECT * FROM orders WHERE id = $1") == []
        assert _candidates("SELECT * FROM orders WHERE customer_id = $1") == []

    def test_an_is_not_null_on_an_indexed_column_names_the_index(self):
        assert _candidates("SELECT * FROM orders WHERE archived_by IS NOT NULL") == [
            (
                "orders.archived_by",
                VIA_NOT_NULL_TEST,
                REFUSED,
                (ALREADY_A_KEY, NO_SELECTIVITY_SIGNAL),
            )
        ]

    def test_a_partial_index_backs_an_is_not_null_test(self):
        assert _candidates("SELECT * FROM orders WHERE claimed_by IS NOT NULL") == [
            ("orders.claimed_by", VIA_NOT_NULL_TEST, ACCEPTED, ())
        ]

    def test_a_category_column(self):
        assert _candidates("SELECT * FROM orders WHERE status = $1") == [
            ("orders.status", VIA_EQUALITY, REFUSED, (NOT_KEY_ELIGIBLE,))
        ]

    def test_a_later_column_of_a_composite_key(self):
        assert _candidates("SELECT * FROM orders WHERE line_no = $1") == [
            ("orders.line_no", VIA_EQUALITY, REFUSED, (COMPOSITE_TAIL,))
        ]

    def test_the_column_the_write_rewrites(self):
        assert _candidates("UPDATE orders SET email = $1 WHERE email = $2", "UPDATE") == [
            ("orders.email", VIA_EQUALITY, REFUSED, (REWRITTEN_BY_WRITE,))
        ]

    def test_a_column_filtered_two_ways_is_one_row_per_filter(self):
        """#1361 review: each filter keeps its own guards, so the IS NOT NULL test's missing
        selectivity signal is never attributed to the equality filter."""
        assert _candidates(
            "UPDATE orders SET email = $1 WHERE email = $2 AND email IS NOT NULL", "UPDATE"
        ) == [
            ("orders.email", VIA_EQUALITY, REFUSED, (REWRITTEN_BY_WRITE,)),
            (
                "orders.email",
                VIA_NOT_NULL_TEST,
                REFUSED,
                (REWRITTEN_BY_WRITE, NO_SELECTIVITY_SIGNAL),
            ),
        ]

    def test_the_evidence_names_the_filter_when_a_column_has_two(self):
        collector, query = _collector(
            "UPDATE orders SET email = $1 WHERE email = $2 AND email IS NOT NULL", "UPDATE"
        )
        evidence = RedesignModel.from_collector(collector).classify(query, "wide-transaction")
        assert (
            "orders.email (an equality filter): " + refusal_sentence([REWRITTEN_BY_WRITE])
        ) in evidence.evidence
        assert (
            "orders.email (an IS NOT NULL test (a sparse index)): "
            + refusal_sentence([REWRITTEN_BY_WRITE, NO_SELECTIVITY_SIGNAL])
        ) in evidence.evidence

    def test_a_keyset_walk_on_an_indexed_column_is_named_not_dropped(self):
        assert _candidates(
            "SELECT * FROM orders WHERE id > $1 AND archived_by IS NOT NULL ORDER BY id LIMIT 10"
        ) == [("orders.archived_by", VIA_NOT_NULL_TEST, REFUSED, (ALREADY_A_KEY, KEYSET_WALK))]

    def test_a_keyset_walk_is_named_not_dropped(self):
        assert _candidates(
            "SELECT * FROM orders WHERE id > $1 AND claimed_by IS NOT NULL ORDER BY id LIMIT 10"
        ) == [("orders.claimed_by", VIA_NOT_NULL_TEST, REFUSED, (KEYSET_WALK,))]

    def test_a_correlated_pin_is_named_not_dropped(self):
        found = _candidates(
            "SELECT * FROM customers c WHERE EXISTS "
            "(SELECT 1 FROM orders o WHERE o.email = c.email)"
        )
        assert ("orders.email", VIA_EQUALITY, REFUSED, (CORRELATED_PIN,)) in found

    @pytest.mark.passes_on_main  # main reads no candidate from it either
    def test_a_statement_it_cannot_read_has_no_candidates(self):
        assert _candidates("SELEC nonsense FROM") == []


@pytest.mark.passes_on_main  # the guard: main adds no key for these either
class TestNoScoringChange:
    """The record is a trace: the keys the redesign assumes, and so every score, are
    those it assumed before (the routing snapshot proves it on the samples)."""

    @pytest.mark.parametrize(
        "text,qtype",
        [
            ("SELECT * FROM orders WHERE archived_by IS NOT NULL", "SELECT"),
            ("UPDATE orders SET email = $1 WHERE email = $2", "UPDATE"),
            ("SELECT * FROM orders WHERE id > $1 AND claimed_by IS NOT NULL", "SELECT"),
        ],
    )
    def test_a_refused_candidate_adds_no_key(self, text, qtype):
        collector, query = _collector(text, qtype)
        model = QueryPatternModel.from_pipeline(collector, {})
        assert model.redesign(query) is None
        assert model.pattern_score(query, "dynamodb") == model.written_pattern_score(
            query, "dynamodb"
        )


def test_the_evidence_and_the_trace_word_a_refusal_with_one_builder(discourse):
    """The anti-pattern evidence and the trace build the refusal from one function."""
    model = RedesignModel.from_collector(discourse)
    query = _query(discourse, SWEEP)
    (candidate,) = model.candidate_keys(query)
    sentence = refusal_sentence(candidate.reasons)
    assert sentence in model.classify(query, WIDE_WRITE).evidence
    assert candidate.explanation().endswith(sentence)


def test_the_discourse_column_filtered_two_ways(discourse):
    """discourse 4994612154416830182 (``WHERE base_scheme_id = $2 AND base_scheme_id IS
    NOT NULL``, an UPDATE of base_scheme_id): one row per filter, each with its own guards."""
    model = RedesignModel.from_collector(discourse)
    rows = {
        c.via: c.reasons
        for c in model.candidate_keys(_query(discourse, "4994612154416830182"))
        if c.label == "color_schemes.base_scheme_id"
    }
    assert set(rows) == {VIA_EQUALITY, VIA_NOT_NULL_TEST}
    assert NO_SELECTIVITY_SIGNAL not in rows[VIA_EQUALITY]
    assert REWRITTEN_BY_WRITE in rows[VIA_EQUALITY]


def test_the_contract_lists_every_refusal_reason():
    assert set(typing.get_args(KeyRefusal)) == set(REFUSAL_REASONS)


def test_the_trace_rows_pass_the_customer_text_guard(discourse):
    """report.json carries the rows: their codes are allowed, their sentence is plain."""
    from src.tools.validation.customer_text_guard import scan_json

    model = RedesignModel.from_collector(discourse)
    rows = [c.as_record() for c in model.candidate_keys(_query(discourse, SWEEP))]
    assert rows and scan_json({"query_traces": {"queries": [{"key_candidates": rows}]}}) == []
