"""The executive-summary request stays within a fixed token budget (#762).

Since #628 Bedrock and headless (Claude Code) reason over the same
``prepare_synthesis_llm_input`` payload, and its lists grow with the workload:
discourse's 1,654 queries already made it about 54k estimated tokens, and a
workload of about 6k queries passed a 200k-token window. The budget is applied
to that shared payload, so both modes still see identical data; these tests
pin what the cap may and may not change.

The large workload is generated here, in code (``_synthetic_det``), never
checked in.
"""

from __future__ import annotations

import copy
import json
import random
from types import SimpleNamespace

import pytest

from src.agents.referee import summary_input_budget as budget_mod
from src.agents.referee.summary_input_budget import (
    CHARS_PER_TOKEN,
    SUMMARY_INPUT_LINE_BUDGET,
    SUMMARY_INPUT_TOKEN_BUDGET,
    TOTAL_FIELD_NAMES,
    apply_summary_input_budget,
    estimate_tokens,
    headless_lines,
    headless_text,
    table_query_weights,
)
from src.agents.referee.synthesis_handler import prepare_synthesis_llm_input

ENGINES = ("aurora_postgresql", "dynamodb", "documentdb", "opensearch")


def _synthetic_det(n_queries: int = 6000, n_tables: int = 1200, n_groups: int = 1000) -> dict:
    """A deterministic-synthesis result shaped like a scaled-up discourse run.

    Seeded, so the same arguments always build the same workload: queries are
    spread unevenly over groups (so ordering by workload share is a real
    choice), every group touches one to three tables, every risk carries a long
    query and table list, and every wave lists every table it moves.
    """
    rng = random.Random(762)
    db = "shop"
    tables = [f"t{i:04d}" for i in range(n_tables)]
    queries = [f"{rng.getrandbits(64):016x}" for _ in range(n_queries)]
    by_group: dict[int, list[str]] = {g: [] for g in range(n_groups)}
    for i, q in enumerate(queries):
        # Zipf-ish: low group numbers carry most queries.
        g = min(int(rng.paretovariate(1.2)) - 1, n_groups - 1) if i % 3 else i % n_groups
        by_group[g].append(q)
    query_groups = []
    query_assignments = []
    engine_tables: dict[str, set[str]] = {e: set() for e in ENGINES}
    engine_queries: dict[str, int] = {e: 0 for e in ENGINES}
    for g, qids in by_group.items():
        engine = ENGINES[g % len(ENGINES)]
        group_tables = sorted(
            {tables[(g * k + 7) % n_tables] for k in range(1, 2 + g % 3)}, reverse=True
        )
        engine_tables[engine].update(group_tables)
        engine_queries[engine] += len(qids)
        query_assignments += [
            {
                "query_id": q,
                "assigned_engine": engine,
                "in_scope": True,
                "source_tables": [f"{db}.{t}" for t in group_tables],
            }
            for q in qids
        ]
        query_groups.append(
            {
                "group_name": ", ".join(f"{db}.{t}" for t in group_tables),
                "engines": [engine],
                "access_patterns": [
                    {
                        "engine": engine,
                        "table_name": f"{db}.{t}",
                        "query_ids": qids,
                        "design_rps": round(rng.uniform(0.1, 40), 1),
                        "description": "point lookup by key " * 4,
                    }
                    for t in group_tables
                ],
            }
        )
    ranking = [
        {
            "target": e,
            "assigned_queries": engine_queries[e],
            "workload_percent": round(100 * engine_queries[e] / n_queries, 1),
            "routed_confidence": 80 - i,
            "schema_design_available": False,
            "assignment_reason_summary": [f"highest fit for {e}"],
        }
        for i, e in enumerate(ENGINES)
    ]

    def _risk(i: int, engine: str) -> dict:
        return {
            "risk_id": f"RISK-{i:03d}",
            "risk_type": "PERFORMANCE_DEGRADATION",
            "severity": ("HIGH", "MEDIUM", "LOW")[i % 3],
            "description": f"[{engine}] multi-table joins at high frequency",
            "affected_tables": [f"{db}.{t}" for t in tables[i::3]],
            "mitigation": "Load-test the flagged queries before cutover.",
            "query_ids": queries[i::4],
        }

    risks = [_risk(i, ENGINES[i % 4]) for i in range(6)]
    resolved = [
        {
            "engine": ENGINES[i % 4],
            "severity": "MEDIUM",
            "description": f"[{ENGINES[i % 4]}] joins resolved by schema design",
            "affected_tables": [f"{db}.{t}" for t in tables[i::2]],
            "query_ids": queries[i::3],
            "resolved_on": "aurora_postgresql",
            "reason": "the queries run as SQL on Aurora PostgreSQL",
        }
        for i in range(9)
    ]
    waves = [
        {
            "wave": w + 1,
            "title": f"Move to {e}",
            "engines": [e],
            "moves_from": ["postgresql"],
            "serves_from": [],
            "tables": [f"{db}.{t}" for t in sorted(engine_tables[e])],
            "table_count": len(engine_tables[e]),
            "table_groups": [
                {
                    "tables": [f"{db}.{t}" for t in sorted(engine_tables[e])],
                    "query_count": engine_queries[e],
                    "kind": "independent",
                }
            ],
            "table_owners": [
                {"table": f"{db}.{t}", "owner": "aurora_postgresql", "sync": "CDC"}
                for t in sorted(engine_tables[e])
            ],
            "query_count": engine_queries[e],
            "workload_share_percent": ranking[w]["workload_percent"],
            "share_basis": "queries",
            "rationale": "one deterministic sequencing rule",
            "gate": "query parity confirmed per table group",
        }
        for w, e in enumerate(ENGINES)
    ]
    return {
        "effective_architecture": {
            "rule": "Every engine and table claim must match effective_architecture.",
            "source_database": db,
            "engines": [
                {
                    "engine": e,
                    "display_name": e,
                    "tables": sorted(engine_tables[e]),
                    "top_query_groups": [],
                    "assigned_queries": engine_queries[e],
                    "workload_percent": ranking[i]["workload_percent"],
                    "capabilities": [f"highest fit for {e}"],
                }
                for i, e in enumerate(ENGINES)
            ],
            "recommended_engine_by_table": {t: ENGINES[i % 4] for i, t in enumerate(tables)},
            "eliminated_engines": [],
        },
        "summary": f"Analyzed {n_tables} source tables and {n_queries} queries.",
        "ranking": ranking,
        "query_groups": query_groups,
        "tco_analysis": {"current_monthly_cost": 0, "projected_monthly_cost": 512.3},
        "risk_assessment": {
            "overall_risk_level": "HIGH",
            "risks": risks,
            "mitigation_strategies": [f"RISK-{i:03d}: load-test" for i in range(6)],
            "resolved_risks": resolved,
        },
        "table_mappings": [
            {
                "source_table": f"{db}.{t}",
                "recommended_database": ENGINES[i % 4],
                "target_table": t.upper(),
                "aggregate_pattern": "separate",
                "confidence_score": 80,
                "alternatives": [],
            }
            for i, t in enumerate(tables)
        ],
        "trade_offs": [
            {
                "description": "Consolidated OpenSearch into Aurora PostgreSQL",
                "impact": "one fewer engine",
                "source_tables": [f"{db}.{t}" for t in tables],
                "target_tables": [],
                "query_ids": queries[:2000],
                "engine": "reality-check",
            }
        ],
        "migration_waves": waves,
        "source_database": "PostgreSQL 16.10",
        # The SynthesisData stand-in: only its assignment is read here.
        "data": SimpleNamespace(assignment={"query_assignments": query_assignments}),
    }


def _small_det() -> dict:
    return _synthetic_det(n_queries=60, n_tables=20, n_groups=12)


@pytest.fixture(scope="module")
def large() -> tuple[dict, dict]:
    det = _synthetic_det()
    return det, prepare_synthesis_llm_input(copy.deepcopy(det))


def _uncapped(det: dict) -> dict:
    """The request as it would be with no budget at all."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(budget_mod, "SUMMARY_INPUT_TOKEN_BUDGET", 10**9)
        mp.setattr(budget_mod, "SUMMARY_INPUT_LINE_BUDGET", 10**9)
        return prepare_synthesis_llm_input(copy.deepcopy(det))


class TestBudget:
    def test_a_6k_query_workload_needs_the_cap(self, large):
        det, _ = large
        assert estimate_tokens(_uncapped(det)) > 4 * SUMMARY_INPUT_TOKEN_BUDGET

    def test_a_6k_query_workload_stays_under_the_budget(self, large):
        _, payload = large
        assert estimate_tokens(payload) <= SUMMARY_INPUT_TOKEN_BUDGET
        status = payload["input_budget"]
        assert status["within_budget"] is True
        # An upper bound: the status is measured with its widest values.
        assert estimate_tokens(payload) <= status["estimated_tokens"] <= SUMMARY_INPUT_TOKEN_BUDGET
        assert status["estimated_tokens_uncapped"] > SUMMARY_INPUT_TOKEN_BUDGET
        assert status["budget_tokens"] == SUMMARY_INPUT_TOKEN_BUDGET

    def test_the_budget_is_measured_on_the_form_headless_reads(self, large):
        """``llm_input.json`` (indent=2) is the larger form: within the budget
        there, within it in the compact Bedrock prompt too, and short enough
        for one default Read page."""
        _, payload = large
        written = headless_text(payload)
        assert written == json.dumps(payload, indent=2, default=str)
        compact = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)
        assert len(compact) < len(written)
        assert estimate_tokens(payload) * CHARS_PER_TOKEN >= len(written)
        assert headless_lines(payload) <= SUMMARY_INPUT_LINE_BUDGET
        assert payload["input_budget"]["lines"] == headless_lines(payload)

    def test_a_long_request_is_cut_for_its_lines_alone(self):
        many_short = {"query_groups": [], "x": list(range(3000))}
        assert estimate_tokens(many_short) <= SUMMARY_INPUT_TOKEN_BUDGET
        assert headless_lines(many_short) > SUMMARY_INPUT_LINE_BUDGET
        assert "input_budget" in apply_summary_input_budget(many_short)

    def test_ten_times_larger_still_fits(self):
        payload = prepare_synthesis_llm_input(
            _synthetic_det(n_queries=60000, n_tables=6000, n_groups=8000)
        )
        assert estimate_tokens(payload) <= SUMMARY_INPUT_TOKEN_BUDGET
        assert payload["input_budget"]["within_budget"] is True

    def test_the_budget_has_room_in_a_200k_window(self):
        # The request plus the skill's instruction text must leave most of the
        # smallest context window this pipeline targets free.
        assert SUMMARY_INPUT_TOKEN_BUDGET <= 200_000 // 5


class TestStatusOnlyWhenCapping:
    def test_a_small_workload_is_passed_whole_with_no_status(self):
        det = _small_det()
        payload = prepare_synthesis_llm_input(copy.deepcopy(det))
        assert "input_budget" not in payload
        assert payload == _uncapped(det)
        assert len(payload["query_groups"]) == len(det["query_groups"])

    def test_no_total_fields_appear_when_nothing_is_capped(self):
        payload = prepare_synthesis_llm_input(_small_det())
        assert "_total" not in repr(sorted(_keys(payload)))

    def test_a_capped_request_says_what_was_capped(self, large):
        det, payload = large
        capped = {c["path"]: c for c in payload["input_budget"]["capped_lists"]}
        groups = capped["query_groups"]
        assert groups["items_total"] == len(det["query_groups"])
        assert groups["items_kept"] == len(payload["query_groups"])
        assert groups["items_kept"] < groups["items_total"]
        for c in capped.values():
            assert c["items_kept"] < c["items_total"]
            assert c["lists"] >= 1

    def test_the_cap_is_logged(self, capsys):
        prepare_synthesis_llm_input(_synthetic_det())
        out = capsys.readouterr().out
        assert "[synthesis] Summary input capped" in out

    def test_no_log_line_without_a_cap(self, capsys):
        prepare_synthesis_llm_input(_small_det())
        assert "capped" not in capsys.readouterr().out


def _keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


class TestQuotedFiguresSurvive:
    """Every figure the summary may quote is the same with or without the cap."""

    def test_untouched_sections(self, large):
        det, payload = large
        whole = _uncapped(det)
        for key in (
            "ranking",
            "tco_analysis",
            "deterministic_summary",
            "schema_design_status",
            "source_database",
        ):
            assert payload[key] == whole[key], key

    def test_wave_figures(self, large):
        det, payload = large
        whole = _uncapped(det)
        assert len(payload["migration_waves"]) == len(whole["migration_waves"])
        for capped, full in zip(payload["migration_waves"], whole["migration_waves"], strict=True):
            for k, v in full.items():
                if not isinstance(v, list):
                    assert capped[k] == v, k
            assert capped["tables_total"] == len(full["tables"]) == full["table_count"]

    def test_engine_figures(self, large):
        det, payload = large
        whole = _uncapped(det)
        engines = payload["effective_architecture"]["engines"]
        for capped, full in zip(engines, whole["effective_architecture"]["engines"], strict=True):
            for k in ("engine", "assigned_queries", "workload_percent", "capabilities"):
                assert capped[k] == full[k]
            assert capped["tables_total"] == len(full["tables"])
            assert set(capped["tables"]) <= set(full["tables"])

    def test_every_risk_is_kept_with_its_severity_and_counts(self, large):
        det, payload = large
        whole = _uncapped(det)
        for key in ("risks", "resolved_risks"):
            got = payload["risk_assessment"][key]
            full = whole["risk_assessment"][key]
            assert len(got) == len(full)
            for capped, entry in zip(got, full, strict=True):
                assert capped["severity"] == entry["severity"]
                assert capped["description"] == entry["description"]
                assert capped["query_ids_total"] == len(entry["query_ids"])
                assert capped["affected_tables_total"] == len(entry["affected_tables"])
        assert (
            payload["risk_assessment"]["overall_risk_level"]
            == whole["risk_assessment"]["overall_risk_level"]
        )
        assert (
            payload["risk_assessment"]["mitigation_strategies"]
            == whole["risk_assessment"]["mitigation_strategies"]
        )

    def test_kept_groups_are_the_largest_and_unchanged(self, large):
        det, payload = large
        whole = _uncapped(det)["query_groups"]
        kept = payload["query_groups"]
        expected = sorted(whole, key=lambda g: (-g["query_count"], g["group_name"]))[: len(kept)]
        assert [g["group_name"] for g in kept] == [g["group_name"] for g in expected]
        for g, full in zip(kept, expected, strict=True):
            assert g["query_count"] == full["query_count"]
            assert g["calls_per_second_by_engine"] == full["calls_per_second_by_engine"]

    def test_the_omitted_groups_are_aggregated(self, large):
        det, payload = large
        whole = _uncapped(det)["query_groups"]
        (groups,) = [
            c for c in payload["input_budget"]["capped_lists"] if c["path"] == "query_groups"
        ]
        kept = {g["group_name"] for g in payload["query_groups"]}
        omitted = [g for g in whole if g["group_name"] not in kept]
        assert groups["items_total"] - groups["items_kept"] == len(omitted)
        assert groups["omitted_query_count"] == sum(g["query_count"] for g in omitted)


class TestDeterministic:
    def test_same_input_same_request(self):
        assert prepare_synthesis_llm_input(_synthetic_det()) == prepare_synthesis_llm_input(
            _synthetic_det()
        )

    def test_input_order_does_not_change_what_is_kept(self, large):
        det, payload = large
        shuffled = copy.deepcopy(det)
        random.Random(1).shuffle(shuffled["query_groups"])
        for engine in shuffled["effective_architecture"]["engines"]:
            engine["tables"] = list(reversed(engine["tables"]))
        again = prepare_synthesis_llm_input(shuffled)
        assert again["query_groups"] == payload["query_groups"]
        assert again["effective_architecture"] == payload["effective_architecture"]

    def test_apply_is_a_no_op_under_the_budget(self):
        payload = {"query_groups": [], "deterministic_summary": "x"}
        assert apply_summary_input_budget(copy.deepcopy(payload)) == payload


def test_a_request_that_cannot_fit_says_so(capsys):
    payload = prepare_synthesis_llm_input(_small_det())
    capsys.readouterr()
    capped = apply_summary_input_budget(payload, budget=10)
    assert capped["input_budget"]["within_budget"] is False
    assert capped["input_budget"]["list_limit"] == 0
    assert "still over the budget" in capsys.readouterr().out
    # The figures still survive: only lists were cut.
    assert capped["ranking"] == payload["ranking"]


def test_the_status_name_is_an_internal_field_the_summary_may_not_name():
    from src.agents.referee.synthesis_grounding import check_summary_internal_leaks

    (finding,) = check_summary_internal_leaks("The input_budget trimmed the groups.")
    assert finding["high_confidence"]


class TestTableRanking:
    """Tables are ranked by the queries touching them across the whole
    assignment (#771 review): in synthesis the request's query groups are the
    relational engine's, so on discourse every group is Aurora PostgreSQL's."""

    @staticmethod
    def _one_engine_groups() -> tuple[dict, dict[str, int]]:
        det = _synthetic_det()
        det["query_groups"] = [g for g in det["query_groups"] if g["engines"] == [ENGINES[0]]]
        weights = table_query_weights(det["data"].assignment["query_assignments"])
        return det, weights

    def test_other_engines_tables_are_kept_by_their_queries_not_by_name(self):
        det, weights = self._one_engine_groups()
        payload = prepare_synthesis_llm_input(copy.deepcopy(det))
        full = {e["engine"]: e["tables"] for e in det["effective_architecture"]["engines"]}
        for engine in payload["effective_architecture"]["engines"]:
            if engine["engine"] == ENGINES[0]:
                continue
            kept = engine["tables"]
            expected = sorted(full[engine["engine"]], key=lambda t: (-weights.get(t, 0), t))
            assert kept == expected[: len(kept)]
            # Not vacuous: name order would have kept different tables.
            assert kept != sorted(full[engine["engine"]])[: len(kept)]
            assert all(weights.get(t, 0) > 0 for t in kept)

    def test_wave_tables_are_kept_by_their_queries(self):
        det, weights = self._one_engine_groups()
        payload = prepare_synthesis_llm_input(copy.deepcopy(det))
        for wave, full in zip(payload["migration_waves"], det["migration_waves"], strict=True):
            kept = wave["tables"]
            expected = sorted(
                full["tables"], key=lambda t: (-weights.get(t.rsplit(".", 1)[-1], 0), t)
            )
            assert kept == expected[: len(kept)]

    def test_out_of_scope_queries_do_not_count(self):
        weights = table_query_weights(
            [
                {"source_tables": ["db.a", "db.b"]},
                {"source_tables": ["db.a"], "in_scope": False},
                {"source_tables": ["a"]},
            ]
        )
        assert weights == {"a": 2, "b": 1}


def test_query_ids_are_cut_in_sorted_order_whatever_the_input_order(large):
    det, payload = large
    shuffled = copy.deepcopy(det)
    rng = random.Random(3)
    for r in shuffled["risk_assessment"]["risks"] + shuffled["risk_assessment"]["resolved_risks"]:
        rng.shuffle(r["query_ids"])
    rng.shuffle(shuffled["trade_offs"][0]["query_ids"])
    assert prepare_synthesis_llm_input(shuffled) == payload


def test_groups_an_engine_names_as_top_are_always_kept():
    det = _synthetic_det()
    smallest = min(det["query_groups"], key=lambda g: (len(g["access_patterns"][0]["query_ids"])))
    det["effective_architecture"]["engines"][0]["top_query_groups"] = [smallest["group_name"]]
    payload = prepare_synthesis_llm_input(det)
    assert payload["query_groups"][0]["group_name"] == smallest["group_name"]


@pytest.mark.parametrize("name", TOTAL_FIELD_NAMES)
def test_every_total_field_is_an_internal_name(name):
    from src.agents.referee.synthesis_grounding import check_summary_internal_leaks

    (finding,) = check_summary_internal_leaks(f"The {name} says there are more.")
    assert finding["high_confidence"]
