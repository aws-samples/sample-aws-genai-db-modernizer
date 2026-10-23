"""Cache overlay and generic routing fixes (#296).

ElastiCache never owns a query. A hot, bounded read keeps its system-of-record
owner and may carry ``cache_engine``. Exact score ties go to the
source-compatible engine; the first assignment has no other ADR-031 rule for a
tie, so anything else is reported as an unresolved tie, never decided by table
traffic, query count or engine name (#503). The leaderboard signal is a hint
for hot reads only.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.agents.referee.assignment_resolver import AssignmentResolver, break_owner_tie
from src.agents.referee.assignment_validator import AssignmentValidator
from src.agents.referee.cache_overlay import (
    CACHE_MAX_ROWS_AVG,
    CACHE_OVERLAY_ENGINES,
    HOT_READ_MIN_CALLS_PER_SECOND,
    NON_SYSTEM_OF_RECORD_ENGINES,
    WRITE_HEAVY_TABLE_MIN_WRITE_SHARE,
    apply_cache_overlay,
    apply_schema_safety_net,
    cache_eligibility,
    cache_pattern,
    can_own,
    dropped_reason,
    fallback_owner,
    is_locking_read,
    is_write_query,
    no_serviceable_candidate_reason,
    normalize_cache_owners,
    overlay_summary,
    owner_decision,
    refresh_cache_overlay,
    safety_net_note,
    write_heavy_tables,
)
from src.agents.referee.query_shape import locking_clause
from src.agents.referee.tie_break import break_tie
from src.agents.referee.triage import triage
from src.contracts.assignment_models import Assignment, AssignmentStatus, QueryAssignment
from src.contracts.move_ledger import ReasonCode
from src.tools.validation.customer_text_guard import scan_text
from tests.unit.tools.sql._timing import assert_linear


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
        "has_aggregations": False,
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

    @pytest.mark.parametrize(
        "text",
        [
            "SELECT NEXT VALUE FOR [dbo].[OrderNumberSeq] AS [NextOrderNumber]",
            "SELECT order_seq.NEXTVAL FROM DUAL",
            "SELECT nextval('orders_id_seq')",
        ],
    )
    def test_sequence_fetch_is_never_cached(self, text):
        # Every call returns a new value, so a cached answer is wrong (#654).
        q = _q("q", text, cps=50.0, rows=1.0, tables=["unknown"])
        assert cache_pattern(q) is None
        assert cache_eligibility(q, set()) is None

    @pytest.mark.parametrize(
        "text",
        [
            "SELECT c.nextval FROM Counter c WHERE c.ID = @ID",
            "SELECT [Body] FROM [Article] WHERE [Title] = 'next value for'",
        ],
    )
    def test_sequence_lookalike_is_still_a_point_lookup(self, text):
        # #654 review: a column named nextval behind an alias, or the words in a
        # string literal, are not a sequence fetch.
        assert cache_pattern(_q("q", text)) == "point_lookup"

    @pytest.mark.parametrize(
        "text",
        [
            "SELECT TOP (10) r.[ReviewID] FROM [ProductReview] r WHERE r.[ProductID] = @ProductID"
            " ORDER BY r.[CreatedDate] DESC",
            "SELECT TOP 10 [ProductID] FROM [Product] ORDER BY [ModifiedDate] DESC",
            "select top (@n) id from t where a = @a order by b",
            "SELECT [ProductID] FROM [Product] WHERE [CategoryID] = @CategoryID ORDER BY [Name]"
            " OFFSET @Skip ROWS FETCH NEXT 24 ROWS ONLY",
            "SELECT id FROM t ORDER BY score DESC FETCH FIRST 10 ROWS ONLY",
        ],
    )
    def test_tsql_and_ansi_top_n_is_top_n(self, text):
        # #663: TOP (n) ... ORDER BY and OFFSET/FETCH are LIMIT's T-SQL/ANSI spellings,
        # read in the source engine's dialect (the facade's top_n fact, #893).
        assert cache_pattern(_q("q", text, rows=10), "sqlserver") == "top_n"
        assert cache_pattern(_q("q", text, rows=1), "sqlserver") == "point_lookup"

    def test_top_without_order_by_is_not_top_n(self):
        # TOP (n) with no ORDER BY returns arbitrary rows, not the top of anything.
        assert cache_pattern(_q("q", "SELECT TOP (5) * FROM t WHERE a > @a", rows=5)) is None

    def test_column_named_top_is_not_top_n(self):
        assert cache_pattern(_q("q", "SELECT top FROM t WHERE id = ? ORDER BY id", rows=5)) == (
            "point_lookup"
        )

    @pytest.mark.parametrize(
        "text",
        [
            "SELECT [CustomerID], [CartJson] FROM [UserSession] WHERE [SessionToken] = @SessionToken",
            "SELECT customerId FROM carts WHERE sessionId = @sessionId",
            "SELECT [UserID] FROM [Login] WHERE [CsrfToken] = @CsrfToken",
            "SELECT [UserID] FROM [Login] WHERE [CSRFToken] = @p0",
            "SELECT [UserID] FROM [ApiClient] WHERE [JWTToken] = @Jwt",
            "SELECT [ClientID] FROM [ApiClient] WHERE [APIToken] = @Key",
            "SELECT [UserID] FROM [UserSession] WHERE [SessionID] = @Id",
            "SELECT [UserID] FROM [Device] WHERE [RefreshToken] = @RefreshToken",
        ],
    )
    def test_pascal_and_camel_case_session_names_are_session_lookups(self, text):
        # #663: T-SQL's [SessionToken] has no word boundary before "Token". A
        # compound session/token key name in the WHERE clause is a session lookup.
        assert cache_pattern(_q("q", text)) == "session_lookup"

    @pytest.mark.parametrize(
        "text",
        [
            # #679 review: a Session/Token name part that is not a session key.
            "SELECT [SessionTimeout] FROM [AppConfig] WHERE [AppID] = @AppID",
            "SELECT [Value] FROM [AppConfig] WHERE [SessionTimeoutMinutes] = @M",
            "SELECT [TokenCount] FROM [Document] WHERE [DocumentID] = @DocumentID",
            "SELECT [Title] FROM [TrainingSession] WHERE [TrainingSessionID] = @ID",
            "SELECT [Name] FROM [CourseSession] WHERE [CourseID] = @CourseID",
            "SELECT [Kind] FROM [ExpressionToken] WHERE [ExpressionID] = @ID",
            "SELECT [Last4] FROM [PaymentTokenVault] WHERE [CustomerID] = @CustomerID",
            # a query on [UserSession] that is not keyed by a session key
            "SELECT [CustomerID] FROM [UserSession] WHERE [CustomerID] = @CustomerID",
            # the token named only in the select list, keyed by something else
            "SELECT s.[SessionToken] FROM [UserSession] s WHERE s.[CustomerID] = @CustomerID",
        ],
    )
    def test_session_or_token_name_parts_that_are_not_a_session_key(self, text):
        assert cache_pattern(_q("q", text)) == "point_lookup"

    @pytest.mark.parametrize(
        "text",
        [
            # #742 item 4: common token columns beyond the session/auth ones.
            "SELECT [UserID] FROM [AppUser] WHERE [ResetToken] = @p0",
            "SELECT [UserID] FROM [AppUser] WHERE [LoginToken] = @p0",
            "SELECT [UserID] FROM [AppUser] WHERE [RememberToken] = @p0",
            "SELECT [UserID] FROM [AppUser] WHERE [VerificationToken] = @p0",
            "SELECT [UserID] FROM [AppUser] WHERE [ConfirmationToken] = @p0",
            "SELECT [UserID] FROM [AppUser] WHERE [ActivationToken] = @p0",
            "SELECT [UserID] FROM [Invite] WHERE [InviteToken] = @p0",
            "SELECT [UserID] FROM [Invite] WHERE [InvitationToken] = @p0",
            "SELECT [UserID] FROM [AppUser] WHERE [MagicToken] = @p0",
            "SELECT [UserID] FROM [OAuthGrant] WHERE [IdToken] = @p0",
            "SELECT [UserID] FROM [OAuthGrant] WHERE [SsoToken] = @p0",
            "SELECT [UserID] FROM [AppUser] WHERE [OtpToken] = @p0",
            "SELECT userId FROM users WHERE resetToken = @p0",
        ],
    )
    def test_common_token_columns_are_session_lookups(self, text):
        assert cache_pattern(_q("q", text)) == "session_lookup"

    @pytest.mark.parametrize(
        "text",
        [
            # A token that is not a login credential stays a plain lookup.
            "SELECT [Last4] FROM [Card] WHERE [PaymentToken] = @p0",
            "SELECT [UserID] FROM [Device] WHERE [DeviceToken] = @p0",
            "SELECT [Kind] FROM [Lexeme] WHERE [ExpressionToken] = @p0",
            "SELECT [Email] FROM [Subscriber] WHERE [UnsubscribeToken] = @p0",
        ],
    )
    def test_other_token_columns_are_not_session_lookups(self, text):
        assert cache_pattern(_q("q", text)) == "point_lookup"

    @pytest.mark.parametrize(
        "text",
        [
            "SELECT * FROM t WHERE [Sessions] = @x",
            "SELECT * FROM t WHERE [Tokenizer] = @x",
            "SELECT * FROM t WHERE [OBSESSION] = @x",
            "SELECT * FROM t WHERE possession = ?",
        ],
    )
    def test_session_match_still_needs_a_whole_word_part(self, text):
        assert cache_pattern(_q("q", text)) == "point_lookup"

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
        assert not can_own("opensearch", write)  # #303: OpenSearch owns no write either
        assert can_own("dynamodb", write)

    def test_opensearch_can_still_own_a_read(self):
        read = _q("r", "SELECT * FROM t WHERE id = ?", "SELECT")
        assert can_own("opensearch", read)

    def test_non_system_of_record_engines_composes_from_the_shared_buckets(self):
        """#303 review round 1: built from the shared cache/search buckets, not

        its own copy, so it stays correct however ``CACHE_OVERLAY_ENGINES``
        itself is defined (#398 re-exports it from ``src.shared.cache_engines``).
        """
        from src.shared.migration_wave_engines import SEARCH_ENGINES

        assert NON_SYSTEM_OF_RECORD_ENGINES == CACHE_OVERLAY_ENGINES | SEARCH_ENGINES
        assert "opensearch" in NON_SYSTEM_OF_RECORD_ENGINES

    def test_locking_read_is_not_ownable_by_opensearch(self):
        """A locking read needs the same consistency guarantee as a write (#303)."""
        locking = _q("r", "SELECT * FROM t WHERE id = ? FOR UPDATE", "SELECT")
        assert not is_write_query(locking)
        assert is_locking_read(locking)
        assert not can_own("opensearch", locking)
        assert can_own("dynamodb", locking)
        assert can_own("aurora_postgresql", locking)

    @pytest.mark.parametrize(
        "text",
        [
            "SELECT * FROM t WHERE id = ? FOR SHARE",
            "SELECT * FROM t WHERE id = ? LOCK IN SHARE MODE",
        ],
    )
    def test_locking_read_shapes(self, text):
        assert is_locking_read(_q("r", text, "SELECT"))

    def test_plain_read_is_not_a_locking_read(self):
        assert not is_locking_read(_q("r", "SELECT * FROM t WHERE id = ?", "SELECT"))

    @pytest.mark.parametrize(
        "text",
        [
            "WITH w AS (INSERT INTO user_badges(id) VALUES (?) RETURNING id) SELECT * FROM w",
            "UPDATE translation_overrides SET status = ? WHERE id = ?",
            "DELETE FROM user_badges WHERE id IN (SELECT id FROM x)",
            "MERGE INTO t USING s ON t.id = s.id",
            "CALL some_procedure(?)",
            "TRUNCATE TABLE sessions",
            "COPY t FROM 'file.csv'",
            "-- a leading comment\nINSERT INTO t VALUES (?)",
            "/* leading block comment */\nCALL proc(?)",
        ],
    )
    def test_other_typed_dml_counts_as_a_write(self, text):
        """A data-modifying CTE, CALL, TRUNCATE, COPY or a comment-led statement

        all come through the collector as query_type "OTHER" (#303 review
        round 1): still a write, so OpenSearch cannot own it.
        """
        query = _q("w", text, "OTHER")
        assert is_write_query(query)
        assert not can_own("opensearch", query)

    def test_other_typed_non_dml_is_not_a_write(self):
        query = _q("w", "WITH w AS (SELECT 1) SELECT * FROM w", "OTHER")
        assert not is_write_query(query)
        assert can_own("opensearch", query)

    def test_percent_inside_a_quoted_literal_is_not_mistaken_for_dml(self):
        """A literal containing the word "update" must not false-positive."""
        query = _q("w", "SELECT * FROM t WHERE note = 'please update later'", "OTHER")
        assert not is_write_query(query)


class TestWriteCheckReadsTheSourceDialect:
    """#1144: ``is_write_query`` reads an ``OTHER`` statement in the source engine's

    dialect, as the locking-read half of the gate does (#1114): MySQL ``#`` comments
    and PostgreSQL ``$$`` strings hold no statement.
    """

    @pytest.mark.parametrize(
        ("engine", "text"),
        [
            ("mysql", "WITH w AS (SELECT 1) # UPDATE x SET a = 1\nSELECT * FROM w"),
            ("mariadb", "WITH w AS (SELECT 1) # delete from t\nSELECT * FROM w"),
            ("postgresql", "WITH w AS (SELECT $$ INSERT INTO t VALUES (1) $$) SELECT * FROM w"),
            ("postgresql", "WITH w AS (SELECT $tag$ DELETE FROM t $tag$) SELECT * FROM w"),
            ("postgresql", "SELECT '$$ INSERT INTO t VALUES (1) $$' AS note, $$ x $$ AS y"),
            ("postgresql", "SELECT $$ UPDATE t SET a = 1 $$ AS body"),
            # an opener word inside a quoted literal or a comment opens no code body
            ("postgresql", "SELECT 'as $$' AS c FROM t WHERE x = $$ update t set a=1 $$"),
            ("postgresql", "SELECT 'do $$', $$ delete from t $$ FROM t"),
            ("postgresql", "SELECT $1 AS c, $$ insert into t values (1) $$"),
            ("postgresql", "SELECT 1 /* do */ $$ delete from t $$"),
            ("postgresql", "DO $$ BEGIN -- delete from t\n PERFORM 1; /* truncate t */ END $$"),
        ],
    )
    def test_a_keyword_in_a_dialect_comment_or_string_is_no_write(self, engine, text):
        query = _q("w", text, "OTHER")
        assert not is_write_query(query, engine)
        assert can_own("opensearch", query, engine)
        # The dialect is what decides: read without it, the text still looks like a write.
        assert is_write_query(query)

    @pytest.mark.parametrize(
        ("engine", "text"),
        [
            ("mysql", "WITH w AS (SELECT 1) # it's a note\nDELETE FROM t WHERE id = 1"),
            ("postgresql", "WITH w AS (SELECT $$it's$$) INSERT INTO t SELECT * FROM w"),
            ("postgresql", "WITH w AS (SELECT 1 # 2) UPDATE t SET a = 1"),
            ("mysql", "-- note\nCALL proc(?)"),
            ("postgresql", "WITH w AS (INSERT INTO t(id) VALUES (1) RETURNING id) SELECT * FROM w"),
            ("postgresql", "DO $$ BEGIN INSERT INTO t VALUES (1); END $$"),
            ("postgresql", "DO LANGUAGE plpgsql $x$ BEGIN DELETE FROM t; END $x$"),
            (
                "postgresql",
                "CREATE FUNCTION f() RETURNS void AS $$ UPDATE t SET a = 1 $$ LANGUAGE sql",
            ),
            # dynamic SQL and nested dollar quotes inside a body are code too
            ("postgresql", "DO $$ BEGIN EXECUTE 'DELETE FROM t'; END $$"),
            ("postgresql", "DO $$ BEGIN EXECUTE format('INSERT INTO %I VALUES (1)', 't'); END $$"),
            ("postgresql", "CREATE FUNCTION f() AS $$ BEGIN EXECUTE 'truncate t'; END $$"),
            ("postgresql", "DO $a$ BEGIN EXECUTE $b$ INSERT INTO t VALUES (1) $b$; END $a$"),
            ("postgresql", "DO $Fn$ BEGIN DELETE FROM t WHERE id = $1; END $Fn$"),
            ("postgresql", "SELECT $1, 'as $$' AS c FROM t; DO $$ BEGIN DELETE FROM t; END $$"),
        ],
    )
    def test_a_real_write_beside_a_dialect_form_is_still_a_write(self, engine, text):
        query = _q("w", text, "OTHER")
        assert is_write_query(query, engine)
        assert not can_own("opensearch", query, engine)

    def test_write_heavy_tables_reads_each_query_in_the_source_dialect(self):
        fake = _q("f", "WITH w AS (SELECT 1) # UPDATE x SET a = 1\nSELECT * FROM w", "OTHER")
        fake["tables_accessed"] = ["x"]
        fake["calls_per_second"] = 10
        assert write_heavy_tables([fake]) == {"x"}
        assert write_heavy_tables([fake], "mysql") == set()

    def test_the_cost_model_prices_a_commented_keyword_as_a_read(self):
        from src.agents.referee.query_cost import QueryCostModel

        query = _q("f", "WITH w AS (SELECT 1) # UPDATE x SET a = 1\nSELECT * FROM w", "OTHER")
        query["calls_per_second"] = 10
        mysql = QueryCostModel(source_engine="mysql").variable_monthly(query, "dynamodb")
        assert mysql < QueryCostModel(source_engine="").variable_monthly(query, "dynamodb")


# The same cases run against the UI's engine picker (src/ui/src/utils/sqlLocking.js).
LOCKING_CASES = json.loads((Path(__file__).parent / "locking_clause_cases.json").read_text())[
    "cases"
]


class TestOwnershipGateUsesTheLockingClause:
    """#838: the ownership gate (ADR-031 Decision 2 step 3) reads a locking read

    with ``query_shape.locking_clause``, the one definition the shape check uses
    (#799/#840), not a private regex: ``FOR NO KEY UPDATE``, ``FOR KEY SHARE``
    and SQL Server lock hints are locks, and a lock word in a string literal,
    a comment or a quoted identifier is not.
    """

    @pytest.mark.parametrize(
        "case", LOCKING_CASES, ids=[repr(c["text"])[:80] for c in LOCKING_CASES]
    )
    def test_gate_agrees_with_the_shared_cases(self, case):
        read = _q("r", case["text"], "SELECT")
        locked = case["clause"] is not None
        engine = case.get("engine")
        assert locking_clause(case["text"], engine) == case["clause"]
        assert is_locking_read(read, engine) is locked
        assert can_own("opensearch", read, engine) is not locked
        assert can_own("dynamodb", read, engine)
        assert can_own("aurora_postgresql", read, engine)

    def test_the_shared_cases_are_loaded(self):
        assert len(LOCKING_CASES) > 40
        assert any(c["clause"] is None for c in LOCKING_CASES)

    def test_the_shared_cases_cover_the_dialects(self):
        # #1143: the UI reads these same cases, so each dialect form needs one.
        by_engine = {c.get("engine") for c in LOCKING_CASES}
        assert {None, "mysql", "mariadb", "postgresql"} <= by_engine
        texts = {(c["text"], c.get("engine")): c["clause"] for c in LOCKING_CASES}
        assert texts["SELECT * FROM t WHERE id = 1 # FOR UPDATE", "mysql"] is None
        assert texts["SELECT * FROM t WHERE id = 1 # FOR UPDATE", None] == "FOR UPDATE"
        assert texts["SELECT $$ FOR UPDATE $$ FROM t WHERE id = 1", "postgresql"] is None
        assert texts["SELECT * FROM t WHERE note = 'FOR UPDATE' AND id = 1", "mysql"] is None

    @pytest.mark.parametrize(
        "text",
        [
            "SELECT * FROM t WHERE id = $1 FOR NO KEY UPDATE",
            "SELECT * FROM t WHERE id = $1 FOR KEY SHARE",
            "SELECT * FROM orders ORDER BY id LIMIT 1 FOR NO KEY UPDATE",
            "SELECT * FROM [t] WITH (UPDLOCK) WHERE [id] = @p",
            "SELECT * FROM [t] WITH (HOLDLOCK) WHERE [id] = @p",
            "SELECT * FROM [t] WITH (XLOCK) WHERE [id] = @p",
            "SELECT * FROM [t] WITH (SERIALIZABLE) WHERE [id] = @p",
        ],
    )
    def test_forms_the_old_regex_missed_are_not_ownable_by_opensearch(self, text):
        read = _q("r", text, "SELECT")
        assert is_locking_read(read)
        assert not can_own("opensearch", read)

    @pytest.mark.parametrize(
        "text",
        [
            "SELECT * FROM t WHERE note = 'FOR UPDATE' AND id = $1",
            "SELECT * FROM t WHERE id = $1 -- FOR UPDATE",
            "SELECT * FROM t WHERE id = $1 /* LOCK IN SHARE MODE */",
            'SELECT "for share" FROM t WHERE id = $1',
        ],
    )
    def test_a_lock_word_that_is_not_a_lock_keeps_opensearch(self, text):
        read = _q("r", text, "SELECT")
        assert not is_locking_read(read)
        assert can_own("opensearch", read)

    @pytest.mark.parametrize(
        "unit",
        [
            "[",
            "`",
            '"',
            "'",
            "'\\",
            '"\\',
            "/*",
            "*/",
            "--",
            "'[",
            '["',
            "FOR UPDATE OF a,",
            "WITH (",
            "(",
        ],
    )
    def test_the_gate_is_linear(self, unit):
        """#871 review: the gate runs on every ``can_own`` call and in the UI picker,

        so it must stay linear on any input: a run of unterminated ``[`` took
        3.1s at 100k characters when quoted identifiers had their own regex.
        """
        prefix = "SELECT * FROM t WHERE a[1] = 1 FOR UPDATE "
        assert_linear(
            lambda n: prefix + unit * (n // len(unit)),
            lambda t: is_locking_read({"query_text": t, "query_type": "SELECT"}),
        )

    def test_cache_pattern_reads_the_same_lock(self):
        """A locking read is never cached; a lock word in a literal does not block it."""
        assert cache_pattern(_q("q", "SELECT * FROM t WHERE id = $1 FOR NO KEY UPDATE")) is None
        assert cache_pattern(_q("q", "SELECT * FROM [t] WITH (UPDLOCK) WHERE [id] = @p")) is None
        assert cache_pattern(_q("q", "SELECT * FROM t WHERE id = $1 AND note = 'FOR UPDATE'")) == (
            "point_lookup"
        )


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

    def test_customer_pinned_warning_names_the_engine_with_display_engine(self):
        # #459 round 2: the warning used to print the raw engine id.
        assignment = self._assignment()
        assignment["query_assignments"][1]["cache_customer_override"] = True
        schema = {"access_patterns": [{"pattern_id": "p", "source_query_ids": ["a"]}]}
        dropped = apply_schema_safety_net(assignment, schema, [_q("a"), _q("b")])
        assert dropped == []  # kept, not dropped -- a customer pin only warns
        b = assignment["query_assignments"][1]
        assert b["cache_engine"] == "elasticache"
        (warning,) = b["warnings"]
        assert "is cached by ElastiCache at the customer's request" in warning
        assert "the ElastiCache schema design has no in-scope access pattern" in warning
        assert "elasticache" not in warning

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
        # Without before/after (a caller with no snapshot), the note names
        # the drop only, using the display name and "hot read(s)" wording --
        # one term throughout (#459 round 2), matching the reports and the
        # executive summary ("fronts N hot reads").
        assert "owner unchanged" in safety_net_note("elasticache", ["b"])
        note = safety_net_note("elasticache", ["a", "b"])
        assert note.startswith("2 hot reads")
        assert "ElastiCache schema design" in note

    def test_note_singular_without_before_after(self):
        note = safety_net_note("elasticache", ["a"])
        assert note.startswith("1 hot read ")
        assert "it is served by its owner engine only" in note

    def test_note_with_before_and_after_names_assigned_covered_and_remaining_counts(self):
        # #424/#459: 20 reads were assigned at the gate at 83.4% of calls; the
        # safety net drops 10, leaving 10 at 61.3% -- the before/after numbers
        # the report and the gate disagreed on, in plain words with the engine's
        # display name, not the raw id, and "hot reads" throughout.
        before = {"query_count": 20, "call_share_percent": 83.4}
        after = {"query_count": 10, "call_share_percent": 61.3}
        note = safety_net_note("elasticache", [f"q{i}" for i in range(10)], before, after)
        # #1252: one short sentence, both figures labelled.
        assert note == "20 cache candidates, 10 designed; 10 stay on their owner engine."

    def test_note_with_before_and_after_dropping_everything(self):
        # #459 round 2/3: a full drop says "covers none of them", "all N",
        # and "None remain" plainly, not the numerically-correct but
        # easy-to-miss "covers 0 of them", "the other N", "0 hot reads
        # (0.0%) remain."
        before = {"query_count": 2, "call_share_percent": 10.0}
        after = {"query_count": 0, "call_share_percent": 0.0}
        note = safety_net_note("elasticache", ["a", "b"], before, after)
        assert note == "2 cache candidates, none designed; all 2 stay on their owner engine."

    def test_note_with_before_only_treats_the_missing_after_as_a_full_drop(self):
        # #459 round 2/3: the branch is keyed on `before` alone (a caller
        # with no `after` is not production code -- synthesis_data.py always
        # passes both -- but must still produce an honest sentence, not the
        # no-snapshot fallback: "covers none of them" is the correct reading
        # of "no after snapshot given").
        before = {"query_count": 20, "call_share_percent": 83.4}
        note = safety_net_note("elasticache", ["a", "b"], before)
        assert note == "20 cache candidates, none designed; all 20 stay on their owner engine."

    def test_note_singular(self):
        before = {"query_count": 3, "call_share_percent": 10.0}
        after = {"query_count": 2, "call_share_percent": 8.0}
        assert safety_net_note("elasticache", ["a"], before, after) == (
            "3 cache candidates, 2 designed; it stays on its owner engine."
        )


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

    @pytest.mark.usefixtures("table_confidence_scores")  # #728: scores per table
    def test_a_non_aurora_tie_is_not_decided_by_which_engine_has_more_table_traffic(self):
        """#503: before this fix, ``break_owner_tie`` gave a non-Aurora tie
        to whichever engine already served more of the tied query's
        tables' traffic. Here dynamodb ends up carrying far more of that
        traffic (4.0 calls/s on "items", via "heavy") than documentdb (0.2,
        via "bulk1"/"bulk2" on "orders") -- the old rule would hand "tied"
        to dynamodb purely because of that unrelated traffic.

        ADR-031 itself has no rule for this; the maintainer-decided
        placeholder (ADR-031 Decision 12, #503 review item 2) instead looks
        at the SETTLED (non-tied) query count: documentdb settles 2 queries
        here (bulk1, bulk2) before "tied" is resolved, dynamodb settles 1
        (heavy), so documentdb -- not traffic, not alphabetical order --
        wins the placeholder. ``test_tie_break.py`` tests the placeholder
        rule directly, including its hash fallback when counts also tie.
        """
        queries = [
            _q("heavy", tables=["items"], cps=4.0),
            _q("bulk1", tables=["orders"], cps=0.1),
            _q("bulk2", tables=["orders"], cps=0.1),
            _q("tied", tables=["orders", "items"], cps=0.1),
        ]
        analysis = {
            "documentdb": _analysis({"orders": 80, "items": 40}),
            "dynamodb": _analysis({"orders": 40, "items": 80}),
        }
        qa = self._resolve(queries, analysis)
        assert qa["heavy"].assigned_engine == "dynamodb"
        assert {qa["bulk1"].assigned_engine, qa["bulk2"].assigned_engine} == {"documentdb"}
        assert qa["tied"].assigned_engine == "documentdb"
        # Structured, not prose-parsed (#503).
        assert qa["tied"].tied_engines == ["documentdb", "dynamodb"]
        assert "no routing rule decides between them" in qa["tied"].assignment_reason
        assert "ADR-031" not in qa["tied"].assignment_reason
        assert "tie_unresolved" not in qa["tied"].assignment_reason
        # The settled, real decisions never carry a tied_engines flag.
        assert qa["heavy"].tied_engines == []
        assert qa["bulk1"].tied_engines == []

    def test_low_traffic_never_tips_a_tie_to_the_cache(self):
        analysis = {
            "elasticache": _analysis({"users": 70}),
            "dynamodb": _analysis({"users": 70}),
            "documentdb": _analysis({"users": 70}),
        }
        qa = self._resolve([_q("q", cps=0.01)], analysis)["q"]
        # tie_unresolved: no settled workload for either engine (this is the
        # only query), so the placeholder falls back to its hash tiebreak --
        # either engine is a valid, deterministic pick, never the cache.
        assert qa.assigned_engine in {"dynamodb", "documentdb"}
        assert qa.tied_engines == ["documentdb", "dynamodb"]
        assert qa.cache_engine is None

    def test_break_owner_tie_order(self):
        winner0, why0, kind0 = break_owner_tie(["dynamodb", "aurora_mysql"], "aurora_mysql")
        assert (winner0, kind0) == ("aurora_mysql", "source_compatible")
        assert "source-compatible" in why0
        winner, why, tie_kind = break_owner_tie(
            ["documentdb", "dynamodb"],
            None,
            settled_counts={"documentdb": 3, "dynamodb": 1},
            query_id="q",
        )
        assert winner == "documentdb"  # more settled workload, not a name preference
        assert tie_kind == "tie_unresolved"
        assert "no routing rule decides between them" in why
        assert "ADR-031" not in why
        assert "tie_unresolved" not in why
        assert "non-preferential" not in why
        assert "documentdb" not in why  # customer-facing wording uses display names
        # Order of the tied list never matters either.
        winner2, why2, tie_kind2 = break_owner_tie(
            ["dynamodb", "documentdb"],
            None,
            settled_counts={"documentdb": 3, "dynamodb": 1},
            query_id="q",
        )
        assert winner2 == winner
        assert tie_kind2 == "tie_unresolved"
        assert why2 == why

    def test_tie_break_is_independent_of_engine_order(self):
        queries = [_q("q")]
        a = {"dynamodb": _analysis({"users": 70}), "documentdb": _analysis({"users": 70})}
        b = dict(reversed(list(a.items())))
        winner_a = self._resolve(queries, a)["q"].assigned_engine
        winner_b = self._resolve(queries, b)["q"].assigned_engine
        assert winner_a == winner_b
        assert winner_a in {"dynamodb", "documentdb"}


# ---------------------------------------------------------------------------
# fallback_owner: a cache's legacy/pinned query needs a real owner (#549)
# ---------------------------------------------------------------------------


def _fallback_owner(
    query, engines, source_engine="", owner_counts=None, triage=None, collector=None
):
    """Test shim: supplies ``fallback_owner``'s required ``triage``/
    ``collector`` keyword-only arguments, defaulting to "none available" so
    most tests can still call it almost positionally -- the one place the
    bare, unsupervised defaults #549 round-3 review warns production code
    away from are allowed to live.
    """
    return fallback_owner(
        query, engines, source_engine, owner_counts, triage=triage, collector=collector
    )


class TestFallbackOwner:
    def test_no_candidate_falls_back_to_source_compatible_aurora(self):
        owner, tied, fallback_reason = _fallback_owner(None, [], source_engine="mysql")
        assert (owner, tied) == ("aurora_mysql", [])
        assert fallback_reason == no_serviceable_candidate_reason("aurora_mysql", "mysql")

    def test_no_candidate_with_no_source_uses_the_generic_aurora_placeholder(self):
        """#786: the shared fallback rule (``fallback_aurora_engine``), the same
        one the first assignment uses: with no source there is no
        source-compatible Aurora engine, so the generic placeholder."""
        owner, tied, fallback_reason = _fallback_owner(None, [])
        assert (owner, tied) == ("aurora", [])
        assert fallback_reason == no_serviceable_candidate_reason("aurora", "")

    def test_source_compatible_engine_wins_a_full_tie_with_no_score_data(self):
        """With no analysis to score with, every candidate is tied
        (:func:`_candidate_scores` returns ``None``) -- ``break_tie``'s own
        first rule, not a pre-scoring Aurora short-circuit, is what hands
        this to the source-compatible engine (#549 review, Important 2).
        This is a real, scored (if data-starved) tie, not the "no
        serviceable candidate" fallback, so it carries no fallback reason.
        """
        owner, tied, fallback_reason = _fallback_owner(
            _q("q"), {"aurora_mysql", "dynamodb"}, source_engine="mysql"
        )
        assert (owner, tied) == ("aurora_mysql", [])
        assert fallback_reason is None

    def test_source_compatible_engine_wins_only_an_exact_tie_via_break_tie(self):
        """#549 review, Important 2/Suggestion 3: with real, equal scores for
        aurora_mysql and dynamodb, the source-compatible engine still wins --
        but only because ``break_tie`` says this is an exact tie that
        includes it (ADR-031 Decision 2), not because Aurora is scored at
        all before the comparison.
        """
        query = _q("q", tables=["items"])
        analysis = {"aurora_mysql": _analysis({"items": 70}), "dynamodb": _analysis({"items": 70})}
        owner, tied, fallback_reason = _fallback_owner(query, analysis, source_engine="mysql")
        assert (owner, tied) == ("aurora_mysql", [])
        assert fallback_reason is None

    @pytest.mark.usefixtures("table_confidence_scores")  # #728: scores per table
    def test_aurora_no_longer_wins_before_the_score_is_compared(self):
        """#549 review, Important 2 repro: a mysql source with aurora_mysql
        scoring far WORSE than dynamodb for this query must not still hand
        it to aurora_mysql just because Aurora is a candidate -- ADR-031
        Decision 2 gives the source-compatible engine an exact tie, never an
        outright win regardless of score.
        """
        query = _q("q", tables=["items"])
        analysis = {"aurora_mysql": _analysis({"items": 10}), "dynamodb": _analysis({"items": 90})}
        owner, tied, fallback_reason = _fallback_owner(query, analysis, source_engine="mysql")
        assert (owner, tied) == ("dynamodb", [])
        assert fallback_reason is None

    @pytest.mark.usefixtures("table_confidence_scores")  # #728: scores per table
    def test_a_non_source_compatible_aurora_engine_does_not_win_either(self):
        """#549 review, Important 2 repro: an Oracle source has no Aurora
        dialect of its own (``SOURCE_ENGINE_TO_AURORA`` has no "oracle"
        entry), so aurora_postgresql is not even source-compatible here --
        it must not win just for being an Aurora candidate while documentdb
        scores far higher.
        """
        query = _q("q", tables=["items"])
        analysis = {
            "aurora_postgresql": _analysis({"items": 10}),
            "documentdb": _analysis({"items": 90}),
        }
        owner, tied, fallback_reason = _fallback_owner(query, analysis, source_engine="oracle")
        assert (owner, tied) == ("documentdb", [])
        assert fallback_reason is None

    def test_capability_gate_excludes_an_unserviceable_candidate(self):
        """#549 review, Critical 1 repro: an aggregation query needs the
        "aggregation" capability, which DynamoDB does not have at all
        (``ENGINE_CAPABILITIES``) -- it must not win just because its base
        table confidence (90) beats documentdb's (10). The resolver would
        score dynamodb 0 for lacking the capability and pick documentdb;
        ``fallback_owner`` must agree -- fed the same ``triage.
        query_capabilities`` the resolver itself reads (#549 round-2 review,
        Critical 1: a bare per-query re-detection with no triage/collector on
        hand is NOT enough on its own for a signal-only requirement, but
        "aggregation" here is text-detectable either way).
        """
        query = _q(
            "q",
            text="SELECT category, COUNT(*) FROM items GROUP BY category",
            tables=["items"],
        )
        analysis = {"dynamodb": _analysis({"items": 90}), "documentdb": _analysis({"items": 10})}
        triage_in = {"query_capabilities": {"q": ["aggregation"]}}
        owner, tied, fallback_reason = _fallback_owner(query, analysis, triage=triage_in)
        assert (owner, tied) == ("documentdb", [])
        assert fallback_reason is None

    def test_capability_gate_falls_back_to_collector_signals_without_triage(self):
        """#549 round-2 review, Critical 1: with no triage ``query_capabilities``
        at all, ``fallback_owner`` still derives the capability gate from the
        collector's own signals (``ScoringContext.from_pipeline``) rather than
        a bare text-only guess -- a ``text_search`` signal's "text_pattern_match"
        requirement, which a parameterized ``LIKE ?`` alone would not surface
        from text, excludes dynamodb (no text_pattern_match) and leaves
        aurora_mysql (which has it) as the only serviceable candidate.
        """
        query = _q("q", text="SELECT * FROM items WHERE name LIKE ?", tables=["items"])
        analysis = {"dynamodb": _analysis({"items": 90}), "aurora_mysql": _analysis({"items": 10})}
        triage_in = {
            "signals": [
                {"signal": "text_search", "targets": ["opensearch"], "query_ids": ["q"]},
            ]
        }
        collector = _collector([query])
        owner, tied, fallback_reason = _fallback_owner(
            query, analysis, triage=triage_in, collector=collector
        )
        assert (owner, tied) == ("aurora_mysql", [])
        assert fallback_reason is None

    @pytest.mark.usefixtures("table_confidence_scores")  # #728: scores per table
    def test_real_score_difference_decides_a_non_aurora_tie_not_query_count(self):
        """#549: before this fix, ``fallback_owner`` picked whichever
        candidate already owned more queries, then by name -- it never
        looked at this query's own fitness at all. Here "documentdb" scores
        far higher for "q" (90 vs 10 on its own table "items"), but
        "dynamodb" already owns many more queries. The old rule would hand
        "q" to dynamodb purely because of that unrelated count.
        """
        query = _q("q", tables=["items"])
        analysis = {"dynamodb": _analysis({"items": 10}), "documentdb": _analysis({"items": 90})}
        owner_counts = {"dynamodb": 20, "documentdb": 1}
        owner, tied, fallback_reason = _fallback_owner(query, analysis, owner_counts=owner_counts)
        assert (owner, tied) == ("documentdb", [])
        assert fallback_reason is None

    def test_a_real_score_tie_goes_through_break_tie_not_the_name(self):
        query = _q("q", tables=["items"])
        analysis = {"dynamodb": _analysis({"items": 70}), "documentdb": _analysis({"items": 70})}
        owner_counts = {"dynamodb": 1, "documentdb": 3}
        owner, tied, fallback_reason = _fallback_owner(query, analysis, owner_counts=owner_counts)
        expected_winner, expected_kind = break_tie(
            ["documentdb", "dynamodb"], set(), None, owner_counts, "q"
        )
        assert owner == expected_winner == "documentdb"  # more settled workload, not a name sort
        assert expected_kind == "tie_unresolved"
        assert tied == ["documentdb", "dynamodb"]
        assert fallback_reason is None

    def test_no_analysis_at_all_treats_every_candidate_as_tied(self):
        """``engines`` is a plain name set here (no per-engine analysis to
        score with), the shape ``normalize_cache_owners``'s synthesis-time
        caller passes. With no fitness signal, every surviving candidate is
        tied -- resolved by ``break_tie``'s placeholder, never by whichever
        name happens to sort first.
        """
        query = _q("q", tables=["items"])
        owner_counts = {"dynamodb": 1, "documentdb": 5}
        owner, tied, fallback_reason = _fallback_owner(
            query, {"dynamodb", "documentdb"}, owner_counts=owner_counts
        )
        expected_winner, expected_kind = break_tie(
            ["documentdb", "dynamodb"], set(), None, owner_counts, "q"
        )
        assert owner == expected_winner == "documentdb"
        assert expected_kind == "tie_unresolved"
        assert tied == ["documentdb", "dynamodb"]
        assert fallback_reason is None

    def test_partial_analysis_coverage_also_falls_back_to_a_tie(self):
        """One candidate's analysis is missing (not just empty) -- comparing
        a real score for one engine against a silent 0 for the other would
        not be a fair comparison, so this is treated the same as having no
        score for either.
        """
        query = _q("q", tables=["items"])
        analysis = {"dynamodb": _analysis({"items": 90}), "documentdb": {}}  # not analyzed yet
        owner, tied, fallback_reason = _fallback_owner(
            query, analysis, owner_counts={"dynamodb": 1, "documentdb": 1}
        )
        assert owner in {"dynamodb", "documentdb"}
        assert tied == ["documentdb", "dynamodb"]
        assert fallback_reason is None

    def test_single_remaining_candidate_is_not_a_tie(self):
        owner, tied, fallback_reason = _fallback_owner(_q("q"), {"dynamodb"})
        assert (owner, tied) == ("dynamodb", [])
        assert fallback_reason is None

    @pytest.mark.usefixtures("table_confidence_scores")  # #728: scores per table
    def test_penalty_review_round2_repro(self):
        """#549 round-2 review, Important 1 repro: dynamodb's base confidence
        (90) alone beats documentdb's (70), but a ``multi-index-joins``
        anti-pattern on this query (penalty 25, ``ANTI_PATTERN_PENALTIES``)
        knocks it down to 65 -- the resolver's own anti-pattern penalty,
        which ``fallback_owner`` must apply too, not just the raw base.
        """
        query = _q("q1", tables=["items"])
        analysis = {
            "dynamodb": {
                "table_recommendations": [{"table_id": "items", "confidence_score": 90}],
                "workload_analysis": {
                    "anti_patterns_detected": [
                        {"anti_pattern_type": "multi-index-joins", "query_ids": ["q1"]}
                    ],
                },
            },
            "documentdb": _analysis({"items": 70}),
        }
        owner, tied, fallback_reason = _fallback_owner(query, analysis)
        assert (owner, tied) == ("documentdb", [])
        assert fallback_reason is None

    def test_no_serviceable_candidate_falls_back_to_source_compatible_aurora(self):
        """#549 round-2 review, Suggestion 3, and round-3 review, Important 1:
        when every candidate fails the exclusion/capability gate, there is no
        serviceable owner among them at all -- this is not a tie among
        engines already known unable to serve the query, so the relational
        baseline wins, the same answer the "no candidate" case returns,
        never one of the excluded engines. Neither dynamodb nor opensearch
        has the "complex_joins" capability (only the Aurora engines do,
        ``ENGINE_CAPABILITIES``). The caller is told this was a fallback,
        not a real scored pick, with a complete, customer-facing sentence
        naming the engine by its display name.
        """
        query = _q("q", tables=["items"])
        analysis = {"dynamodb": _analysis({"items": 90}), "opensearch": _analysis({"items": 10})}
        triage_in = {"query_capabilities": {"q": ["complex_joins"]}}
        owner, tied, fallback_reason = _fallback_owner(
            query, analysis, source_engine="mysql", triage=triage_in
        )
        assert (owner, tied) == ("aurora_mysql", [])
        assert fallback_reason == (
            "No analyzed engine can serve this query; it moves to Aurora MySQL, which "
            "runs the source SQL as is."
        )


# ---------------------------------------------------------------------------
# normalize_cache_owners: the recorded owner carries the real tie (#549)
# ---------------------------------------------------------------------------


class TestNormalizeCacheOwnersTie:
    def test_a_non_aurora_tie_is_recorded_on_the_query(self):
        assignment = {
            "query_assignments": [
                {
                    "query_id": "q",
                    "assigned_engine": "elasticache",
                    "assignment_reason": "legacy",
                }
            ]
        }
        queries = [_q("q", tables=["items"], cps=0.01)]
        analysis = {"dynamodb": _analysis({"items": 70}), "documentdb": _analysis({"items": 70})}
        normalize_cache_owners(assignment, queries, analysis, triage=None, collector=None)
        qa = assignment["query_assignments"][0]
        assert qa["assigned_engine"] in {"dynamodb", "documentdb"}
        assert qa["tied_engines"] == ["documentdb", "dynamodb"]

    @pytest.mark.usefixtures("table_confidence_scores")  # #728: scores per table
    def test_a_clear_score_winner_carries_no_tie(self):
        """dynamodb sorts AFTER documentdb alphabetically, so making it the
        clear winner (#549 review, Suggestion 2) means this can only pass on
        the score -- the old count-then-name rule would have had no reason
        to prefer it either.
        """
        assignment = {
            "query_assignments": [
                {
                    "query_id": "q",
                    "assigned_engine": "elasticache",
                    "assignment_reason": "legacy",
                }
            ]
        }
        queries = [_q("q", tables=["items"], cps=0.01)]
        analysis = {"dynamodb": _analysis({"items": 90}), "documentdb": _analysis({"items": 10})}
        normalize_cache_owners(assignment, queries, analysis, triage=None, collector=None)
        qa = assignment["query_assignments"][0]
        assert qa["assigned_engine"] == "dynamodb"
        assert qa["tied_engines"] == []


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

    def test_tsql_top_n_fires_the_signal(self):
        # #663: a SQL Server source's top-N reads are spelled TOP (n) ... ORDER BY.
        text = "SELECT TOP (10) [ReviewID] FROM [ProductReview] ORDER BY [CreatedDate] DESC"
        signal = self._signal([_q("hot", text, cps=2.0), _q("cold", text, cps=0.01)])
        assert signal is not None and signal.query_ids == ["hot"]


class TestTriageSessionStore:
    def _signal(self, queries):
        result = triage(_collector(queries, engine="sqlserver"))
        return next((s for s in result.signals if s.signal == "session_store"), None)

    def test_tsql_session_token_column_fires_the_signal(self):
        # #663: triage lowercases the SQL for its other checks; the session match
        # must see the original case to split [SessionToken].
        text = "SELECT [CustomerID] FROM [UserSession] WHERE [SessionToken] = @SessionToken"
        signal = self._signal(
            [_q("s", text), _q("other", "SELECT * FROM [Brand] WHERE [ID] = @ID")]
        )
        assert signal is not None and signal.query_ids == ["s"]

    def test_lookalike_names_do_not_fire_the_signal(self):
        # #679 review: session_store selects analysis agents on its own, so it stays
        # conservative: no signal for a config read or a non-session UserSession query.
        signal = self._signal(
            [
                _q("cfg", "SELECT [SessionTimeout] FROM [AppConfig] WHERE [AppID] = @AppID"),
                _q("cnt", "SELECT [TokenCount] FROM [Document] WHERE [DocumentID] = @ID"),
                _q("trn", "SELECT [Title] FROM [TrainingSession] WHERE [ID] = @ID"),
                _q("vlt", "SELECT [Last4] FROM [PaymentTokenVault] WHERE [CustomerID] = @C"),
                _q("usr", "DELETE TOP (5000) FROM [UserSession] WHERE [ExpiresAt] < @Now"),
            ]
        )
        assert signal is None


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

    def test_opensearch_write_is_a_hard_error(self):
        """A customer edit putting a write on OpenSearch is rejected (#303)."""
        write = _q("q", "UPDATE users SET name = ? WHERE id = ?", "UPDATE")
        result = AssignmentValidator().validate(
            self._assignment("opensearch"), _collector([write]), {"opensearch": {}}
        )
        assert not result.valid
        assert any("is a write owned by OpenSearch," in e for e in result.errors)

    def test_opensearch_read_is_still_valid(self):
        result = AssignmentValidator().validate(
            self._assignment("opensearch"), _collector([_q("q")]), {"opensearch": {}}
        )
        assert result.valid


class TestDroppedReason:
    """#572 review item 6: ``dropped_reason`` used to interpolate the raw engine
    id (``cache_reason`` is a customer-facing field) -- checked here with the
    guard itself, not just a hand-picked substring, so a future regression in
    either the wording or the guard's own detectors is caught."""

    def test_uses_the_display_name(self) -> None:
        assert dropped_reason("elasticache") == (
            "cache overlay dropped after schema design: no in-scope ElastiCache "
            "access pattern serves it (owner unchanged)"
        )

    def test_has_no_internal_wording(self) -> None:
        for engine in ("elasticache", "dynamodb", "documentdb", "opensearch"):
            assert scan_text(dropped_reason(engine)) == []


def test_triage_names_the_top_n_spellings():
    """#679 review: triage's evidence text names the TOP, FETCH and LIMIT spellings."""
    text = "SELECT TOP (10) [ReviewID] FROM [ProductReview] ORDER BY [CreatedDate] DESC"
    result = triage(_collector([_q("hot", text, cps=2.0)], engine="sqlserver"))
    signal = next(s for s in result.signals if s.signal == "leaderboard_pattern")
    assert "ORDER BY + LIMIT, TOP or FETCH" in signal.evidence


class TestOwnerDecisionNeverTiesAGatedEngine:
    """#812: with no analysis to score with, every owner candidate is tied,
    but only the ones the shared scorer's gates let serve the query (ADR-031
    Decision 2 steps 2-3, Decision 12): a gated engine is never listed as tied."""

    _AGGREGATION = "SELECT customer_id, COUNT(*) FROM orders GROUP BY customer_id"

    def test_a_capability_gated_engine_is_left_out_of_the_tie(self):
        query = _q("q", text=self._AGGREGATION, tables=["orders"])
        triage = {"query_capabilities": {"q": ["aggregation"]}}
        decision = owner_decision(
            query,
            {"documentdb", "dynamodb", "opensearch"},
            triage=triage,
            collector=None,
        )
        assert decision.tied_engines == ["documentdb", "opensearch"]
        assert decision.owner in {"documentdb", "opensearch"}
        assert decision.gates == {"dynamodb": "capability"}

    def test_a_hard_excluded_engine_is_left_out_of_the_tie(self):
        query = _q("q", text="SELECT id FROM posts WHERE title LIKE '%term%'", tables=["posts"])
        decision = owner_decision(
            query, {"documentdb", "dynamodb"}, triage={"query_capabilities": {}}, collector=None
        )
        assert (decision.owner, decision.tied_engines) == ("documentdb", [])
        assert decision.gates == {"dynamodb": "hard_exclusion"}

    def test_an_empty_analysis_is_no_analysis_and_still_gated(self):
        query = _q("q", text=self._AGGREGATION, tables=["orders"])
        decision = owner_decision(
            query,
            {"documentdb": {}, "dynamodb": {}},
            triage={"query_capabilities": {"q": ["aggregation"]}},
            collector=None,
        )
        assert (decision.owner, decision.tied_engines) == ("documentdb", [])

    def test_every_candidate_gated_is_the_no_score_fallback(self):
        query = _q("q", text=self._AGGREGATION, tables=["orders"])
        decision = owner_decision(
            query,
            {"dynamodb"},
            source_engine="mysql",
            triage={"query_capabilities": {"q": ["aggregation"]}},
            collector=None,
        )
        assert (decision.owner, decision.tied_engines) == ("aurora_mysql", [])
        assert decision.fallback_rule == ReasonCode.FALLBACK_NO_SCORE


class TestWriteDetectionWithNestedComments:
    """#825: ``is_write_query`` and its UI copy cannot know the source dialect (the
    ownership gate ``can_own`` sees only the query), so they read the leading
    comments both ways -- nested (PostgreSQL, SQL Server) and ending at the first
    ``*/`` (MySQL, Oracle) -- and a write under either reading is a write."""

    @pytest.mark.parametrize(
        "text",
        [
            "/* a /* b */ c */ TRUNCATE t",
            "/* a /* b */ c */ COPY t FROM STDIN",
            "/* a /* b */ c */ CALL proc()",
            "/* x */ /* a /* b */ c */ -- d\nCALL proc()",
        ],
    )
    def test_a_nested_leading_comment_does_not_hide_the_verb(self, text):
        query = _q("w", text, "OTHER")
        assert is_write_query(query)
        assert not can_own("opensearch", query)

    def test_a_nested_comment_holding_dml_text_in_a_read_is_still_a_read_when_closed_both_ways(
        self,
    ):
        query = _q("r", "/* insert into x */ WITH w AS (SELECT 1) SELECT * FROM w", "OTHER")
        assert not is_write_query(query)

    @pytest.mark.parametrize(
        "text",
        [
            "/* delete from /* x */ SELECT 1",
            "/* insert into t /* x */ SELECT * FROM t",
        ],
    )
    def test_an_unterminated_nested_comment_stays_a_read(self, text):
        """The nested reading is ignored when it strips less than the first-close one."""
        query = _q("r", text, "OTHER")
        assert not is_write_query(query)
        assert can_own("opensearch", query)

    @pytest.mark.slow
    def test_a_long_run_of_openers_stays_linear(self):
        from tests.unit.tools.sql._timing import assert_linear

        assert_linear(
            lambda n: "/*" * (n // 2) + " CALL p()",
            lambda text: is_write_query(_q("w", text, "OTHER")),
        )


@pytest.mark.parametrize(
    "text",
    [
        "UPDATE posts p SET views = 1 WHERE p.id = 2",
        "UPDATE posts AS p SET views = 1 WHERE p.id = 2",
        'UPDATE "public"."posts" "p" SET views = 1',
        "UPDATE ONLY posts p SET views = 1",
        "WITH a AS (SELECT 1) UPDATE posts p SET views = 1 FROM a",
        "WITH a AS (SELECT 1) UPDATE posts AS p SET views = 1 FROM a",
        "UPDATE posts SET views = 1",
    ],
)
def test_an_update_with_or_without_a_table_alias_is_a_write(text: str) -> None:
    """``UPDATE <table> <alias> SET`` was missed by ``update\\s+\\S+\\s+set`` (#1287)."""
    query = {"query_type": "OTHER", "query_text": text}
    assert is_write_query(query, "postgresql")


@pytest.mark.parametrize(
    "text",
    [
        "SELECT * FROM posts p WHERE p.note = 'UPDATE posts p SET x = 1'",
        "ALTER TABLE t ADD CONSTRAINT fk FOREIGN KEY (a) REFERENCES u (a) ON UPDATE SET NULL",
        "WITH a AS (SELECT 1) SELECT * FROM a",
    ],
)
@pytest.mark.passes_on_main
def test_text_that_only_mentions_update_is_no_write(text: str) -> None:
    query = {"query_type": "OTHER", "query_text": text}
    assert not is_write_query(query, "postgresql")
