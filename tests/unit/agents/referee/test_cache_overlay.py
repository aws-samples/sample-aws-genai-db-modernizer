"""Cache overlay and generic routing fixes (#296).

ElastiCache never owns a query. A hot, bounded read keeps its system-of-record
owner and may carry ``cache_engine``. Exact score ties go to the
source-compatible engine, then the engine serving more of the tables' traffic,
then more queries, then name. The leaderboard signal is a hint for hot reads only.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.agents.referee.assignment_resolver import (
    AssignmentResolver,
    break_owner_tie,
    enforce_exclusions_on_overrides,
)
from src.agents.referee.assignment_validator import AssignmentValidator
from src.agents.referee.cache_overlay import (
    CACHE_MAX_ROWS_AVG,
    HOT_READ_MIN_CALLS_PER_SECOND,
    WRITE_HEAVY_TABLE_MIN_WRITE_SHARE,
    apply_cache_overlay,
    apply_schema_safety_net,
    cache_eligibility,
    cache_pattern,
    can_own,
    overlay_summary,
    refresh_cache_overlay,
    safety_net_note,
    write_heavy_tables,
)
from src.agents.referee.triage import triage
from src.contracts.assignment_models import (
    Assignment,
    AssignmentStatus,
    QueryAssignment,
)


def _q(
    qid: str,
    text: str = "SELECT * FROM users WHERE id = ?",
    qtype: str = "SELECT",
    cps: float = 5.0,
    rows: float = 1.0,
    tables: list[str] | None = None,
    **extra,
) -> dict:
    return {
        "query_id": qid,
        "query_text": text,
        "query_type": qtype,
        "calls_per_second": cps,
        "rows_returned_avg": rows,
        "tables_accessed": tables if tables is not None else ["users"],
        "join_count": 0,
        "has_joins": False,
        "has_aggregation": False,
        "filter_tables": [],
        **extra,
    }


def _collector(queries: list[dict], engine: str = "") -> dict:
    tables = sorted({t for q in queries for t in q["tables_accessed"]})
    out = {
        "job_id": "t",
        "database_schema": {"tables": [{"table_id": t, "table_name": t} for t in tables]},
        "queries": {"query_patterns": queries},
    }
    if engine:
        out["metadata"] = {"source_database": {"engine": engine}}
    return out


def _analysis(conf: dict[str, int]) -> dict:
    return {
        "table_recommendations": [{"table_id": t, "confidence_score": c} for t, c in conf.items()],
        "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
    }


def _triage(engines: list[str], signals: list[dict] | None = None) -> dict:
    return {"selected_agents": [{"agent_type": e} for e in engines], "signals": signals or []}


def _by_id(assignment: Assignment) -> dict[str, QueryAssignment]:
    return {qa.query_id: qa for qa in assignment.query_assignments}


# ---------------------------------------------------------------------------
# Eligibility rules
# ---------------------------------------------------------------------------


class TestEligibility:
    def test_hot_point_lookup_qualifies(self):
        pattern, reason = cache_eligibility(_q("q", cps=3.27), set())
        assert pattern == "point_lookup"
        assert "3.3 calls/s" in reason

    def test_threshold_is_one_call_per_second(self):
        assert HOT_READ_MIN_CALLS_PER_SECOND == 1.0
        assert cache_eligibility(_q("q", cps=1.0), set()) is not None
        assert cache_eligibility(_q("q", cps=0.99), set()) is None

    def test_low_traffic_read_never_qualifies_whatever_its_shape(self):
        # A Rails .first at 0.1 calls/s has the top-N shape and is not cache material
        top_n = 'SELECT * FROM "users" WHERE "users"."id" = $1 ORDER BY id LIMIT 1'
        assert cache_eligibility(_q("q", top_n, cps=0.1), set()) is None
        assert cache_eligibility(_q("q", top_n, cps=0.1), set(), ["leaderboard_pattern"]) is None

    def test_writes_never_qualify(self):
        for qtype in ("INSERT", "UPDATE", "DELETE"):
            assert (
                cache_eligibility(_q("q", "UPDATE users SET a = ?", qtype, cps=50), set()) is None
            )

    def test_unbounded_result_does_not_qualify(self):
        assert cache_eligibility(_q("q", rows=CACHE_MAX_ROWS_AVG), set()) is not None
        assert cache_eligibility(_q("q", rows=CACHE_MAX_ROWS_AVG + 1), set()) is None

    def test_write_heavy_table_does_not_qualify(self):
        assert cache_eligibility(_q("q"), {"users"}) is None

    @pytest.mark.parametrize(
        "text",
        [
            "SELECT status, COUNT(*) FROM users WHERE a = ? GROUP BY status",
            "SELECT SUM(total) FROM orders o JOIN items i ON o.id = i.order_id WHERE o.id = ?",
            "SELECT * FROM users WHERE name LIKE ?",
            "SELECT * FROM users WHERE id = ? FOR UPDATE",
            "SELECT * FROM users WHERE created_at > ?",  # a range scan is not a lookup
        ],
    )
    def test_non_lookup_shapes_do_not_qualify(self, text):
        assert cache_eligibility(_q("q", text), set()) is None

    def test_shapes(self):
        assert cache_pattern(_q("q", "SELECT * FROM t ORDER BY score DESC LIMIT 10", rows=10)) == (
            "top_n"
        )
        assert cache_pattern(_q("q", "SELECT * FROM t WHERE id = ? ORDER BY id LIMIT 1")) == (
            "point_lookup"
        )
        assert cache_pattern(_q("q", "SELECT * FROM sessions WHERE session_id = ?")) == (
            "session_lookup"
        )
        assert cache_pattern(_q("q", "SELECT * FROM tax_classes ORDER BY name")) == (
            "reference_read"
        )
        assert cache_pattern(_q("q", "SELECT * FROM t WHERE id IN (...)")) == "point_lookup"

    def test_write_heavy_tables(self):
        queries = [
            _q("r", cps=1.0, tables=["a", "b"]),
            _q("w", "UPDATE a SET x = ?", "UPDATE", cps=1.0, tables=["a"]),
            _q("w2", "INSERT INTO b VALUES (?)", "INSERT", cps=0.5, tables=["b"]),
        ]
        assert WRITE_HEAVY_TABLE_MIN_WRITE_SHARE == 0.5
        assert write_heavy_tables(queries) == {"a"}  # a: 50% writes, b: 33%


class TestOwnership:
    def test_cache_never_owns(self):
        assert not can_own("elasticache")
        assert not can_own("elasticache", _q("q"))

    def test_write_gate(self):
        write = _q("w", "INSERT INTO t VALUES (?)", "INSERT")
        assert not can_own("elasticache", write)
        assert can_own("dynamodb", write)
        assert can_own("opensearch", write)  # its write gate is #303


# ---------------------------------------------------------------------------
# Overlay application and summary
# ---------------------------------------------------------------------------


class TestOverlay:
    def test_no_cache_engine_means_no_overlay(self):
        qas = apply_cache_overlay(
            [{"query_id": "q", "assigned_engine": "dynamodb"}], [_q("q")], ["dynamodb"]
        )
        assert qas[0]["cache_engine"] is None

    def test_overlay_keeps_the_owner(self):
        qas = apply_cache_overlay(
            [{"query_id": "q", "assigned_engine": "dynamodb"}],
            [_q("q")],
            ["dynamodb", "elasticache"],
        )
        assert qas[0]["assigned_engine"] == "dynamodb"
        assert qas[0]["cache_engine"] == "elasticache"
        assert qas[0]["cache_pattern"] == "point_lookup"

    def test_reevaluation_clears_a_query_that_no_longer_qualifies(self):
        qa = {"query_id": "q", "assigned_engine": "dynamodb", "cache_engine": "elasticache"}
        apply_cache_overlay([qa], [_q("q", cps=0.2)], ["dynamodb", "elasticache"])
        assert qa["cache_engine"] is None and qa["cache_reason"] is None

    def test_summary_counts_in_scope_queries_and_call_share(self):
        queries = [_q("hot", cps=3.0), _q("cold", cps=1.0), _q("out", cps=6.0)]
        qas = [
            {"query_id": "hot", "assigned_engine": "dynamodb", "cache_engine": "elasticache"},
            {"query_id": "cold", "assigned_engine": "aurora_mysql"},
            {
                "query_id": "out",
                "assigned_engine": "dynamodb",
                "cache_engine": "elasticache",
                "in_scope": False,
            },
        ]
        summary = overlay_summary(qas, queries)
        assert summary["query_count"] == 1
        assert summary["call_share_percent"] == 75.0  # 3 of the 4 in-scope calls/s
        assert summary["owners"] == {"dynamodb": 1}
        assert summary["min_calls_per_second"] == HOT_READ_MIN_CALLS_PER_SECOND

    def test_summary_is_none_without_cached_queries(self):
        assert overlay_summary([{"query_id": "q", "assigned_engine": "x"}], [_q("q")]) is None

    def test_refresh_sets_summary(self):
        assignment = {"query_assignments": [{"query_id": "q", "assigned_engine": "dynamodb"}]}
        refresh_cache_overlay(assignment, [_q("q")], ["dynamodb", "elasticache"])
        assert assignment["cache_overlay"]["query_count"] == 1


class TestSafetyNet:
    def _assignment(self) -> dict:
        return {
            "query_assignments": [
                {"query_id": "a", "assigned_engine": "dynamodb", "cache_engine": "elasticache"},
                {"query_id": "b", "assigned_engine": "aurora_mysql", "cache_engine": "elasticache"},
            ]
        }

    def test_uncovered_overlay_is_dropped_owner_unchanged(self):
        assignment = self._assignment()
        schema = {"access_patterns": [{"pattern_id": "p", "source_query_ids": ["a"]}]}
        dropped = apply_schema_safety_net(assignment, schema, [_q("a"), _q("b")])
        assert dropped == ["b"]
        b = assignment["query_assignments"][1]
        assert b["cache_engine"] is None and b["assigned_engine"] == "aurora_mysql"
        assert assignment["cache_overlay"]["query_count"] == 1

    def test_out_of_scope_access_pattern_does_not_cover(self):
        assignment = self._assignment()
        schema = {
            "access_patterns": [{"pattern_id": "p", "source_query_ids": ["a"], "in_scope": False}]
        }
        assert apply_schema_safety_net(assignment, schema, []) == ["a", "b"]

    def test_no_design_drops_nothing(self):
        assignment = self._assignment()
        assert apply_schema_safety_net(assignment, None, []) == []
        assert apply_schema_safety_net(assignment, {}, []) == []
        assert assignment["query_assignments"][0]["cache_engine"] == "elasticache"

    def test_note(self):
        assert "owner unchanged" in safety_net_note("elasticache", ["b"])
        assert safety_net_note("elasticache", ["a", "b"]).startswith("2 cached queries")


# ---------------------------------------------------------------------------
# Resolver: ownership, overlay, leaderboard hint
# ---------------------------------------------------------------------------


class TestResolverOverlay:
    def test_elasticache_never_owns_even_with_the_best_score(self):
        queries = [_q("hot"), _q("cold", cps=0.01)]
        analysis = {
            "dynamodb": _analysis({"users": 60}),
            "elasticache": _analysis({"users": 95}),
        }
        result = AssignmentResolver().resolve(
            _triage(["dynamodb", "elasticache"]), analysis, _collector(queries)
        )
        qa = _by_id(result)
        assert {q.assigned_engine for q in qa.values()} == {"dynamodb"}
        assert qa["hot"].cache_engine == "elasticache"
        assert qa["cold"].cache_engine is None  # low traffic never tips to the cache
        assert result.cache_overlay is not None
        assert result.cache_overlay.query_count == 1

    def test_leaderboard_signal_is_not_an_ownership_override(self):
        queries = [_q("lb", "SELECT * FROM users ORDER BY score DESC LIMIT 10", cps=0.01)]
        triage_out = _triage(
            ["dynamodb", "elasticache"],
            [{"signal": "leaderboard_pattern", "targets": ["elasticache"], "query_ids": ["lb"]}],
        )
        analysis = {"dynamodb": _analysis({"users": 50}), "elasticache": _analysis({"users": 50})}
        qa = _by_id(AssignmentResolver().resolve(triage_out, analysis, _collector(queries)))["lb"]
        assert qa.assigned_engine == "dynamodb"
        assert qa.signal_override is None
        assert qa.cache_engine is None

    def test_session_store_signal_is_not_an_ownership_override(self):
        queries = [_q("s", "SELECT * FROM sessions WHERE token = ?", tables=["sessions"])]
        triage_out = _triage(
            ["aurora_mysql", "elasticache"],
            [{"signal": "session_store", "targets": ["elasticache"], "query_ids": ["s"]}],
        )
        analysis = {
            "aurora_mysql": _analysis({"sessions": 40}),
            "elasticache": _analysis({"sessions": 90}),
        }
        qa = _by_id(AssignmentResolver().resolve(triage_out, analysis, _collector(queries)))["s"]
        assert qa.assigned_engine == "aurora_mysql"
        assert (qa.cache_engine, qa.cache_pattern) == ("elasticache", "session_lookup")

    def test_only_elasticache_analyzed_falls_back_to_aurora(self):
        result = AssignmentResolver().resolve(
            _triage(["aurora_mysql", "elasticache"]),
            {"elasticache": _analysis({"users": 90})},
            _collector([_q("q")], engine="mysql"),
        )
        assert _by_id(result)["q"].assigned_engine == "aurora_mysql"

    def test_co_dependency_group_never_goes_to_the_cache(self):
        queries = [
            _q("g1", "SELECT * FROM a JOIN b ON a.id = b.a WHERE a.id = ?", tables=["a", "b"]),
            _q("g2", "SELECT * FROM a JOIN b ON a.id = b.a WHERE b.id = ?", tables=["a", "b"]),
        ]
        for q in queries:
            q.update(has_joins=True, join_count=2)
        analysis = {
            "dynamodb": _analysis({"a": 50, "b": 50}),
            "elasticache": _analysis({"a": 99, "b": 99}),
        }
        result = AssignmentResolver().resolve(
            _triage(["dynamodb", "elasticache"]), analysis, _collector(queries)
        )
        assert {qa.assigned_engine for qa in result.query_assignments} == {"dynamodb"}

    def test_customer_override_reassignment_never_picks_the_cache(self):
        queries = [_q("q", "SELECT * FROM t WHERE body LIKE '%term%'")]
        assignment = Assignment(
            job_id="t",
            version=2,
            status=AssignmentStatus.CUSTOMER_MODIFIED,
            timestamp=datetime.now(UTC),
            query_assignments=[
                QueryAssignment(
                    query_id="q",
                    assigned_engine="dynamodb",
                    confidence=50,
                    source_tables=["t"],
                    assignment_reason="customer",
                )
            ],
            table_assignments=[],
            co_dependency_groups=[],
            validation_warnings=[],
        )
        fixed = enforce_exclusions_on_overrides(
            assignment, queries, {"dynamodb": {}, "elasticache": {}, "opensearch": {}}
        )
        assert fixed.query_assignments[0].assigned_engine == "opensearch"


# ---------------------------------------------------------------------------
# Resolver: exact-tie break
# ---------------------------------------------------------------------------


class TestTieBreak:
    def _resolve(self, queries, analysis, engine=""):
        engines = list(analysis)
        return _by_id(
            AssignmentResolver().resolve(_triage(engines), analysis, _collector(queries, engine))
        )

    def test_tie_goes_to_the_source_compatible_engine(self):
        analysis = {
            "dynamodb": _analysis({"users": 70}),
            "aurora_postgresql": _analysis({"users": 70}),
        }
        qa = self._resolve([_q("q")], analysis, engine="postgresql")["q"]
        assert qa.assigned_engine == "aurora_postgresql"
        assert "source-compatible" in qa.assignment_reason

    def test_tie_goes_to_the_engine_serving_more_of_the_tables_traffic(self):
        # "warm" puts orders traffic on documentdb; "tied" ties documentdb/dynamodb
        queries = [
            _q("warm", tables=["orders"], cps=4.0),
            _q("bulk1", tables=["items"], cps=0.1),
            _q("bulk2", tables=["items"], cps=0.1),
            _q("tied", tables=["orders", "items"], cps=0.1),
        ]
        analysis = {
            "documentdb": _analysis({"orders": 80, "items": 40}),
            "dynamodb": _analysis({"orders": 40, "items": 80}),
        }
        qa = self._resolve(queries, analysis)
        assert qa["warm"].assigned_engine == "documentdb"
        assert {qa["bulk1"].assigned_engine, qa["bulk2"].assigned_engine} == {"dynamodb"}
        # dynamodb serves more queries (2) but documentdb more of the traffic (4.0 calls/s)
        assert qa["tied"].assigned_engine == "documentdb"
        assert "traffic" in qa["tied"].assignment_reason

    def test_low_traffic_never_tips_a_tie_to_the_cache(self):
        analysis = {
            "elasticache": _analysis({"users": 70}),
            "dynamodb": _analysis({"users": 70}),
            "documentdb": _analysis({"users": 70}),
        }
        qa = self._resolve([_q("q", cps=0.01)], analysis)["q"]
        assert qa.assigned_engine == "documentdb"  # name order among owners
        assert qa.cache_engine is None

    def test_break_owner_tie_order(self):
        by_id = {"q": {"tables_accessed": ["t"]}}
        assert break_owner_tie(["dynamodb", "aurora_mysql"], ["q"], by_id, "aurora_mysql", {}, {})[
            0
        ] == ("aurora_mysql")
        traffic = {("t", "dynamodb"): 1.0}
        assert break_owner_tie(["documentdb", "dynamodb"], ["q"], by_id, None, traffic, {}) == (
            "dynamodb",
            "serves more of these tables' traffic (1.00 calls/s)",
        )
        assert break_owner_tie(
            ["documentdb", "dynamodb"], ["q"], by_id, None, {}, {"dynamodb": 3}
        ) == ("dynamodb", "serves more queries")
        assert break_owner_tie(["dynamodb", "documentdb"], ["q"], by_id, None, {}, {}) == (
            "documentdb",
            "name order",
        )

    def test_tie_break_is_independent_of_engine_order(self):
        queries = [_q("q")]
        a = {"dynamodb": _analysis({"users": 70}), "documentdb": _analysis({"users": 70})}
        b = dict(reversed(list(a.items())))
        assert self._resolve(queries, a)["q"].assigned_engine == "documentdb"
        assert self._resolve(queries, b)["q"].assigned_engine == "documentdb"


# ---------------------------------------------------------------------------
# Triage and validator
# ---------------------------------------------------------------------------


class TestTriageLeaderboard:
    def _signal(self, queries):
        result = triage(_collector(queries, engine="postgresql"))
        return next((s for s in result.signals if s.signal == "leaderboard_pattern"), None)

    def test_only_hot_selects_count(self):
        text = "SELECT * FROM users ORDER BY score DESC LIMIT 10"
        signal = self._signal(
            [
                _q("hot", text, cps=2.0),
                _q("cold", text, cps=0.01),
                _q("upd", "UPDATE t SET a = 1 ORDER BY id LIMIT 1", "UPDATE", cps=5),
            ]
        )
        assert signal is not None and signal.query_ids == ["hot"]

    def test_no_hot_top_n_no_signal(self):
        assert self._signal([_q("cold", "SELECT * FROM u ORDER BY id LIMIT 1", cps=0.5)]) is None


class TestValidator:
    def _assignment(self, engine: str, qid: str = "q") -> Assignment:
        return Assignment(
            job_id="t",
            version=1,
            status=AssignmentStatus.CUSTOMER_MODIFIED,
            timestamp=datetime.now(UTC),
            query_assignments=[
                QueryAssignment(
                    query_id=qid,
                    assigned_engine=engine,
                    confidence=50,
                    source_tables=["users"],
                    assignment_reason="customer",
                )
            ],
            table_assignments=[],
            co_dependency_groups=[],
            validation_warnings=[],
        )

    def test_cache_owner_is_a_hard_error(self):
        result = AssignmentValidator().validate(
            self._assignment("elasticache"), _collector([_q("q")]), {"elasticache": {}}
        )
        assert not result.valid
        assert any("cache layer" in e for e in result.errors)

    def test_owner_engine_is_valid(self):
        result = AssignmentValidator().validate(
            self._assignment("dynamodb"), _collector([_q("q")]), {"dynamodb": {}}
        )
        assert result.valid
