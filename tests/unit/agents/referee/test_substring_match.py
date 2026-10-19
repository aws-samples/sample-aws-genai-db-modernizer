"""A leading-wildcard LIKE is an unindexed substring match (#493, option C).

Maintainer decision (2026-10-16): penalise a literal leading-wildcard LIKE as a scan only
when the scan matters (a frequent query on a large table); a word search
(``'%word%'`` on a text column) gets redesign guidance instead; every engine's reason is
stated in the decision trace and the deliverables; a parameterised ``LIKE $1`` stays
unpenalised (#912). Aurora MySQL and DocumentDB lack a substring index (they scan),
Aurora PostgreSQL has one (``pg_trgm``, an extension to create), OpenSearch serves it
natively: ADR-031 Decision 2 step 5 holds, a penalty only lands on an engine that lacks
the capability.
"""

from __future__ import annotations

import pytest

from src.agents.referee.assignment_resolver import AssignmentResolver
from src.agents.referee.query_scoring import ScoringContext, merge_substring_scan
from src.agents.referee.substring_match import (
    engine_reason,
    scan_adjustments,
    substring_match_risks,
    substring_matches,
)
from src.shared.catalog.patterns import capability_engines, pattern, scan_engines
from src.shared.engine_names import ENGINE_DISPLAY_NAMES, display_engine
from src.tools.sql.query_facts import query_facts

SCAN = ("aurora_mysql", "documentdb")
NATIVE = ("aurora_postgresql", "opensearch")
DESCRIBED = (*NATIVE, *SCAN)
SUFFIX = "SELECT id FROM users WHERE email LIKE '%@example.com'"
WORD = "SELECT id FROM posts WHERE title LIKE '%term%'"


def _threshold():
    t = pattern("substring-match").scan_threshold
    assert t is not None
    return t


def _collector(
    text: str = SUFFIX,
    *,
    cps: float | None = None,
    rows: int | None = None,
    column_type: str = "varchar(255)",
    source: str = "mysql",
) -> dict:
    t = _threshold()
    cps = t.min_calls_per_second if cps is None else cps
    rows = t.min_table_rows if rows is None else rows
    return {
        "job_id": "t",
        "metadata": {"source_database": {"engine": source}},
        "database_schema": {
            "tables": [
                {
                    "table_id": "app.users",
                    "table_name": "users",
                    "row_count": rows,
                    "columns": [
                        {"column_name": "id", "data_type": "bigint"},
                        {"column_name": "email", "data_type": column_type},
                        {"column_name": "title", "data_type": column_type},
                    ],
                }
            ]
        },
        "queries": {
            "query_patterns": [
                {
                    "query_id": "q1",
                    "query_text": text,
                    "query_type": "SELECT",
                    "tables_accessed": ["app.users"],
                    "calls_per_second": cps,
                    "has_joins": False,
                    "join_count": 0,
                    "has_aggregations": False,
                    "filter_tables": [],
                }
            ]
        },
    }


def _penalties(collector: dict) -> dict[str, int]:
    penalties, _ = scan_adjustments(collector)
    return {engine: penalties.get(("q1", engine), 0) for engine in DESCRIBED}


class TestTheCatalogModelsItAsItsOwnPattern:
    def test_mysql_and_documentdb_scan_postgresql_and_opensearch_index_it(self) -> None:
        assert scan_engines("substring-match") == frozenset(SCAN)
        assert capability_engines("substring_index") == frozenset(NATIVE)

    def test_the_thresholds_are_defined_once_with_a_sourced_why(self) -> None:
        t = _threshold()
        assert t.min_calls_per_second > 0 and t.min_table_rows > 0
        assert "MySQL Reference Manual" in t.why

    def test_each_reason_names_no_engine_display_names_are_generated(self) -> None:
        for support in pattern("substring-match").engines.values():
            for text in (support.why, support.guidance):
                for name in ENGINE_DISPLAY_NAMES.values():
                    assert name not in text, (name, text)

    def test_the_four_reasons(self) -> None:
        match = substring_matches(_collector(WORD))["q1"]
        reasons = {e: engine_reason(e, match) or "" for e in DESCRIBED}
        assert "pg_trgm" in reasons["aurora_postgresql"]
        assert "extension to create" in reasons["aurora_postgresql"]
        assert "FULLTEXT" in reasons["aurora_mysql"] and "word-based" in reasons["aurora_mysql"]
        assert "$regex" in reasons["documentdb"] and "$text" in reasons["documentdb"]
        assert "natively" in reasons["opensearch"]
        for engine, reason in reasons.items():
            assert reason.startswith(display_engine(engine))
        assert engine_reason("dynamodb", match) is None


class TestTheFacts:
    @pytest.mark.parametrize(
        ("text", "word"),
        [
            (WORD, True),
            (SUFFIX, False),
            ("SELECT 1 FROM t WHERE title LIKE CONCAT('%', ?, '%')", True),
            ("SELECT 1 FROM t WHERE [Body] LIKE N'%x%'", True),
            ("SELECT 1 FROM t WHERE [Name] LIKE '%' + @Term + '%'", True),
            ("SELECT 1 FROM t WHERE title LIKE '%' || $1", False),
            ("SELECT 1 FROM t WHERE title NOT ILIKE '%a_b%'", False),
        ],
    )
    def test_a_leading_wildcard_and_its_shape(self, text: str, word: bool) -> None:
        assert query_facts(text).leading_wildcard_like
        likes = query_facts(text).wildcard_likes
        assert likes and likes[0].word_search is word

    @pytest.mark.parametrize(
        "text",
        [
            "SELECT 1 FROM t WHERE title LIKE $1",
            "SELECT 1 FROM t WHERE title LIKE ?",
            "SELECT 1 FROM t WHERE title LIKE 'abc%'",
            "SELECT 1 FROM t WHERE title LIKE CONCAT(?, '%')",
        ],
    )
    def test_a_parameterised_or_prefix_pattern_is_not_one(self, text: str) -> None:
        assert not query_facts(text).leading_wildcard_like
        assert query_facts(text).wildcard_likes == ()


class TestThePenaltyOnlyWhenTheScanMatters:
    def test_a_frequent_substring_match_on_a_large_table_costs_the_scan_engines(self) -> None:
        penalty = pattern("substring-match").penalty
        assert _penalties(_collector()) == {
            "aurora_postgresql": 0,
            "opensearch": 0,
            "aurora_mysql": penalty,
            "documentdb": penalty,
        }

    def test_a_rare_query_costs_nothing(self) -> None:
        rare = _threshold().min_calls_per_second / 2
        assert set(_penalties(_collector(cps=rare)).values()) == {0}

    def test_a_small_table_costs_nothing(self) -> None:
        small = _threshold().min_table_rows - 1
        assert set(_penalties(_collector(rows=small)).values()) == {0}

    def test_a_word_search_costs_nothing_and_gets_the_redesign(self) -> None:
        collector = _collector(WORD)
        assert set(_penalties(collector).values()) == {0}
        _, notes = scan_adjustments(collector)
        assert "MATCH ... AGAINST" in notes[("q1", "aurora_mysql")]
        assert "$text" in notes[("q1", "documentdb")]

    def test_a_word_pattern_on_a_non_text_column_is_a_substring_match(self) -> None:
        collector = _collector("SELECT id FROM users WHERE email LIKE '%42%'", column_type="int")
        assert _penalties(collector)["aurora_mysql"] == pattern("substring-match").penalty

    def test_a_parameterised_pattern_stays_unpenalised(self) -> None:
        """#912: the value of LIKE $1 is unknown."""
        collector = _collector("SELECT id FROM users WHERE email LIKE $1")
        assert scan_adjustments(collector) == ({}, {})

    def test_the_note_states_the_reason_for_the_lower_score(self) -> None:
        _, notes = scan_adjustments(_collector())
        mysql = notes[("q1", "aurora_mysql")]
        assert f"loses {pattern("substring-match").penalty} points" in mysql
        pg = notes[("q1", "aurora_postgresql")]
        assert "pg_trgm" in pg and "Aurora MySQL scores" in pg and "DocumentDB scores" in pg

    def test_below_the_threshold_the_scan_is_stated_as_a_risk(self) -> None:
        _, notes = scan_adjustments(_collector(cps=0.01))
        assert "Not penalised" in notes[("q1", "aurora_mysql")]


class TestTheScorerMergesIt:
    def test_from_pipeline_carries_the_scan_penalty_and_its_note(self) -> None:
        analysis: dict[str, dict] = {e: {"workload_analysis": {}} for e in DESCRIBED}
        ctx = ScoringContext.from_pipeline(analysis, {"query_capabilities": {}}, _collector())
        assert ctx.anti_pattern_map[("q1", "aurora_mysql")] == pattern("substring-match").penalty
        assert "FULLTEXT" in ctx.anti_pattern_notes[("q1", "aurora_mysql")]
        assert ("q1", "aurora_postgresql") not in ctx.anti_pattern_map

    def test_a_bigger_penalty_already_on_the_pair_keeps_its_note(self) -> None:
        merged, notes = merge_substring_scan(
            {("q1", "aurora_mysql"): 35}, {("q1", "aurora_mysql"): "other"}, _collector(), SCAN
        )
        assert merged[("q1", "aurora_mysql")] == 35
        assert notes[("q1", "aurora_mysql")] == "other"


def _analysis(score: int, patterns: list[dict] | None = None) -> dict:
    return {
        "table_recommendations": [{"table_id": "app.users", "confidence_score": score}],
        "workload_analysis": {"patterns_detected": patterns or [], "anti_patterns_detected": []},
    }


# OpenSearch scores only the queries its own analysis matched (PATTERN_ONLY_ENGINES).
_OS_MATCH = [{"pattern_type": "wildcard-search", "query_ids": ["q1"], "table_ids": ["app.users"]}]


class TestAMySQLSource:
    """The case with the most routing risk (review of #911): on a MySQL source the
    source-compatible engine scans a frequent substring match on a large table."""

    def _resolve(self, collector: dict):
        analysis = {"aurora_mysql": _analysis(80), "opensearch": _analysis(70, _OS_MATCH)}
        triage = {
            "selected_agents": [{"agent_type": e} for e in analysis],
            "signals": [],
            "query_capabilities": {"q1": ["text_pattern_match"]},
        }
        return AssignmentResolver().resolve(triage, analysis, collector)

    @pytest.mark.usefixtures("table_confidence_scores")  # #728: scores per table
    def test_aurora_mysql_loses_the_scan_penalty_and_the_reason_is_stated(self) -> None:
        assignment = self._resolve(_collector())
        qa = assignment.query_assignments[0]
        # 80 - 20 = 60 on Aurora MySQL, 70 on OpenSearch: the query moves on the score,
        # and the decision trace says why.
        assert qa.assigned_engine == "opensearch"
        assert "natively" in qa.assignment_reason
        assert "Aurora MySQL scores 20 lower here: Aurora MySQL scans" in qa.assignment_reason

    def test_below_the_threshold_it_stays_on_aurora_mysql_with_the_scan_as_a_risk(self) -> None:
        qa = self._resolve(_collector(cps=0.01)).query_assignments[0]
        assert qa.assigned_engine == "aurora_mysql"
        assert "FULLTEXT" in qa.assignment_reason and "Not penalised" in qa.assignment_reason

    def test_a_word_search_stays_on_aurora_mysql_with_the_redesign(self) -> None:
        qa = self._resolve(_collector(WORD)).query_assignments[0]
        assert qa.assigned_engine == "aurora_mysql"
        assert "MATCH ... AGAINST" in qa.assignment_reason


class TestTheDeliverablesStateItAsARisk:
    def test_a_scan_engine_that_keeps_a_frequent_scan_is_a_medium_risk(self) -> None:
        risks = substring_match_risks(_collector(), {"q1": "aurora_mysql"}, DESCRIBED)
        assert len(risks) == 1
        risk = risks[0]
        assert risk["severity"] == "MEDIUM" and risk["query_ids"] == ["q1"]
        assert risk["description"].startswith("[aurora_mysql] ")
        assert "FULLTEXT" in risk["description"] and "DocumentDB scored 20 lower" in (
            risk["description"]
        )
        assert risk["mitigation"]

    def test_aurora_postgresql_states_the_extension_to_create(self) -> None:
        risk = substring_match_risks(_collector(WORD), {"q1": "aurora_postgresql"}, DESCRIBED)[0]
        assert risk["severity"] == "LOW"
        assert "pg_trgm" in risk["description"] and "CREATE EXTENSION pg_trgm" in risk["mitigation"]

    def test_opensearch_serves_it_natively_with_nothing_to_do(self) -> None:
        assert substring_match_risks(_collector(), {"q1": "opensearch"}, DESCRIBED) == []


class TestEveryStringPassesTheCustomerWordingGuard:
    """Every reason and risk this pattern writes is customer-facing text, held to the
    same guard the e2e wording tests run, with the bare-shape check used for reason fields
    (a product name such as pg_trgm must never read as a reason code)."""

    @pytest.mark.parametrize(
        "collector",
        [_collector(), _collector(cps=0.01), _collector(WORD)],
        ids=["scan", "rare", "word"],
    )
    def test_reasons_notes_and_risks(self, collector: dict) -> None:
        from src.tools.validation.customer_text_guard import scan_json, scan_text

        _, notes = scan_adjustments(collector)
        texts = list(notes.values())
        for engine in DESCRIBED:
            risks = substring_match_risks(collector, {"q1": engine}, DESCRIBED)
            # The leading [engine] tag is the risk's structured engine (scan_json skips it).
            texts += [r["description"].split("] ", 1)[1] for r in risks]
            texts += [r["mitigation"] for r in risks]
            for risk in risks:
                assert scan_json({"risks": [risk]}) == [], risk
        assert texts
        for text in texts:
            assert scan_text(text, strict_reason_codes=False) == [], text


def _schema_collector(
    text: str,
    *,
    tables: list[tuple[str, int | None, list[tuple[str, str]]]],
    accessed: list[str] | None = None,
    cps: float | None = None,
    source: str = "mysql",
) -> dict:
    """A collection of the given ``(name, rows, [(column, type)])`` tables and one query,
    ``q1``, that reads ``accessed`` (every table by default)."""
    t = _threshold()
    return {
        "job_id": "t",
        "metadata": {"source_database": {"engine": source}},
        "database_schema": {
            "tables": [
                {
                    "table_id": f"app.{name}",
                    "table_name": name,
                    "row_count": rows,
                    "columns": [{"column_name": c, "data_type": ty} for c, ty in columns],
                }
                for name, rows, columns in tables
            ]
        },
        "queries": {
            "query_patterns": [
                {
                    "query_id": "q1",
                    "query_text": text,
                    "query_type": "SELECT",
                    "tables_accessed": [f"app.{n}" for n in (accessed or [x[0] for x in tables])],
                    "calls_per_second": t.min_calls_per_second if cps is None else cps,
                }
            ]
        },
    }


BIG = _threshold().min_table_rows * 10
SMALL = 50
JOIN_TABLES = [
    ("orders", BIG, [("id", "bigint"), ("note", "text"), ("code", "varchar(20)")]),
    ("tags", SMALL, [("id", "bigint"), ("label", "varchar(40)"), ("code", "int")]),
]
JOIN = "SELECT * FROM orders o JOIN tags t ON t.id = o.id WHERE {} LIKE '%x.y'"


class TestTheScannedTableAndColumnAreRead:
    """#942: the row count is the table that holds the LIKE column, and the column's type
    is read from the collected schema, not the largest table or a guess."""

    def _match(self, collector: dict):
        return substring_matches(collector)["q1"]

    def test_a_search_of_a_small_table_joined_to_a_large_one_does_not_scan_the_large_one(
        self,
    ) -> None:
        match = self._match(_schema_collector(JOIN.format("t.label"), tables=JOIN_TABLES))
        assert (match.table_rows, match.scan_matters, match.approximations) == (SMALL, False, ())
        penalties, _ = scan_adjustments(
            _schema_collector(JOIN.format("t.label"), tables=JOIN_TABLES)
        )
        assert penalties == {}

    def test_a_search_of_the_large_table_is_a_scan_that_matters(self) -> None:
        match = self._match(_schema_collector(JOIN.format("o.note"), tables=JOIN_TABLES))
        assert (match.table_rows, match.scan_matters, match.approximations) == (BIG, True, ())

    def test_an_unqualified_column_is_found_in_the_one_table_that_holds_it(self) -> None:
        match = self._match(_schema_collector(JOIN.format("label"), tables=JOIN_TABLES))
        assert (match.table_rows, match.scan_matters, match.approximations) == (SMALL, False, ())

    def test_the_alias_names_the_table_not_the_table_name_order(self) -> None:
        text = "SELECT * FROM tags o JOIN orders t ON t.id = o.id WHERE o.label LIKE '%x.y'"
        match = self._match(_schema_collector(text, tables=JOIN_TABLES))
        assert (match.table_rows, match.scan_matters) == (SMALL, False)

    def test_the_type_is_the_type_of_the_table_that_holds_the_column(self) -> None:
        """``code`` is text in orders and an integer in tags: a word pattern on tags.code
        is a substring match, on orders.code a word search."""
        word = "SELECT * FROM orders o JOIN tags t ON t.id = o.id WHERE {}.code LIKE '%42%'"
        tags = self._match(_schema_collector(word.format("t"), tables=JOIN_TABLES))
        orders = self._match(_schema_collector(word.format("o"), tables=JOIN_TABLES))
        assert (tags.word_search, orders.word_search) == (False, True)
        # The older reading took the first query table that has a column of that name.
        assert tags.approximations == () and orders.approximations == ()

    def test_a_function_of_the_column_names_its_table_and_reads_the_type_as_text(self) -> None:
        text = "SELECT * FROM orders o JOIN tags t ON t.id = o.id WHERE lower(t.label) LIKE '%x.y'"
        match = self._match(_schema_collector(text, tables=JOIN_TABLES))
        assert (match.table_rows, match.scan_matters) == (SMALL, False)
        word = text.replace("%x.y", "%word%")
        wrapped = self._match(_schema_collector(word, tables=JOIN_TABLES))
        assert wrapped.word_search is True
        assert any("wraps column label" in n for n in wrapped.approximations)

    def test_a_column_with_no_collected_type_is_read_as_text_and_says_so(self) -> None:
        tables = [("tags", SMALL, [("id", "bigint"), ("label", "")])]
        match = self._match(
            _schema_collector("SELECT 1 FROM tags WHERE label LIKE '%word%'", tables=tables)
        )
        assert match.word_search is True
        assert any("label has no collected type" in n for n in match.approximations)

    def test_a_traced_table_with_no_row_count_says_the_scan_cannot_be_sized(self) -> None:
        tables: list[tuple[str, int | None, list[tuple[str, str]]]] = [
            ("tags", None, [("id", "bigint"), ("label", "varchar(40)")])
        ]
        text = "SELECT 1 FROM tags WHERE label LIKE '%x.y'"
        collector = _schema_collector(text, tables=tables)
        match = self._match(collector)
        assert (match.table_rows, match.scan_matters) == (None, False)
        assert "table tags has no collected row count" in match.approximations[0]
        _, notes = scan_adjustments(collector)
        assert (
            "Read approximately: table tags has no collected row count"
            in notes[("q1", "aurora_mysql")]
        )

    def test_an_ambiguous_column_keeps_the_older_reading_and_says_so(self) -> None:
        match = self._match(_schema_collector(JOIN.format("id"), tables=JOIN_TABLES))
        assert (match.table_rows, match.scan_matters) == (BIG, True)
        (note,) = match.approximations
        assert "column id was not traced to one collected table (ambiguous" in note
        assert "largest table the query reads counts" in note

    def test_a_column_of_a_derived_table_keeps_the_older_reading_and_says_so(self) -> None:
        text = "SELECT * FROM (SELECT note AS n FROM orders) d WHERE d.n LIKE '%x.y'"
        match = self._match(_schema_collector(text, tables=JOIN_TABLES, accessed=["orders"]))
        assert (match.table_rows, match.scan_matters) == (BIG, True)
        assert len(match.approximations) == 1

    def test_a_table_the_collector_did_not_describe_keeps_the_older_reading(self) -> None:
        text = "SELECT * FROM ghosts WHERE name LIKE '%x.y'"
        match = self._match(_schema_collector(text, tables=JOIN_TABLES, accessed=["orders"]))
        assert (match.table_rows, match.approximations != ()) == (BIG, True)

    def test_an_expression_over_two_columns_keeps_the_older_reading(self) -> None:
        text = "SELECT * FROM orders o JOIN tags t ON t.id = o.id WHERE concat(o.note, t.label) LIKE '%x.y'"
        match = self._match(_schema_collector(text, tables=JOIN_TABLES))
        assert (match.table_rows, match.scan_matters) == (BIG, True)
        assert "names no single column" in match.approximations[0]

    def test_a_statement_the_parser_cannot_read_keeps_the_older_reading(self) -> None:
        text = "SELECT * FROM orders WHERE note LIKE '%x.y' AND ((("
        match = self._match(_schema_collector(text, tables=JOIN_TABLES, accessed=["orders"]))
        assert (match.table_rows, match.scan_matters) == (BIG, True)
        assert match.approximations

    def test_each_predicate_is_read_on_its_own_table(self) -> None:
        both = (
            "SELECT * FROM orders o JOIN tags t ON t.id = o.id "
            "WHERE t.label LIKE '%x.y' AND o.note LIKE '%word%'"
        )
        match = self._match(_schema_collector(both, tables=JOIN_TABLES))
        # The suffix match is on the small table; the word search on the large one is a
        # word search: nothing scans a large table.
        assert (match.word_search, match.table_rows, match.scan_matters) == (False, SMALL, False)
        swapped = both.replace("t.label LIKE '%x.y'", "t.label LIKE '%word%'").replace(
            "o.note LIKE '%word%'", "o.note LIKE '%x.y'"
        )
        match = self._match(_schema_collector(swapped, tables=JOIN_TABLES))
        assert (match.table_rows, match.scan_matters) == (BIG, True)

    def test_the_decision_trace_states_the_approximation_and_a_clean_reading_does_not(
        self,
    ) -> None:
        ambiguous = _schema_collector(JOIN.format("id"), tables=JOIN_TABLES)
        _, notes = scan_adjustments(ambiguous)
        assert "Read approximately: column id was not traced" in notes[("q1", "aurora_mysql")]
        assert "Read approximately" in notes[("q1", "aurora_postgresql")]
        _, clean = scan_adjustments(_schema_collector(JOIN.format("o.note"), tables=JOIN_TABLES))
        assert all("Read approximately" not in n for n in clean.values())

    def test_the_trace_text_passes_the_customer_wording_guard(self) -> None:
        from src.tools.validation.customer_text_guard import scan_text

        for text in (JOIN.format("id"), "SELECT * FROM ghosts WHERE name LIKE '%x.y'"):
            _, notes = scan_adjustments(
                _schema_collector(text, tables=JOIN_TABLES, accessed=["orders"])
            )
            for note in notes.values():
                assert scan_text(note, strict_reason_codes=False) == [], note


class TestARegularExpressionIsTheSameScan:
    """#982: a regular expression no index serves is priced like a wildcard scan."""

    PENALTY = pattern("substring-match").penalty

    def test_a_frequent_regexp_on_a_large_table_costs_the_scan_engines(self) -> None:
        collector = _collector("SELECT id FROM users WHERE email REGEXP 'example'")
        assert _penalties(collector) == {
            "aurora_postgresql": 0,
            "opensearch": 0,
            "aurora_mysql": self.PENALTY,
            "documentdb": self.PENALTY,
        }

    def test_a_rare_or_small_regexp_costs_nothing(self) -> None:
        text = "SELECT id FROM users WHERE email REGEXP 'example'"
        rare = _threshold().min_calls_per_second / 2
        assert set(_penalties(_collector(text, cps=rare)).values()) == {0}
        small = _threshold().min_table_rows - 1
        assert set(_penalties(_collector(text, rows=small)).values()) == {0}

    def test_a_prefix_regex_costs_documentdb_nothing_but_still_scans_mysql(self) -> None:
        collector = _collector("SELECT id FROM users WHERE email ~ '^admin'", source="postgresql")
        assert _penalties(collector) == {
            "aurora_postgresql": 0,
            "opensearch": 0,
            "aurora_mysql": self.PENALTY,
            "documentdb": 0,
        }
        _, notes = scan_adjustments(collector)
        assert "prefix regular expression" in notes[("q1", "documentdb")]

    @pytest.mark.parametrize(
        "text",
        [
            "SELECT id FROM users WHERE email ~* '^admin'",  # case-insensitive: no index
            "SELECT id FROM users WHERE email ~ 'admin'",  # not anchored
            "SELECT id FROM users WHERE email ~ '^.admin'",  # starts with a metacharacter
            "SELECT id FROM users WHERE email ~ '^admin' AND title ~ 'x'",  # one is not
        ],
    )
    def test_any_other_regex_costs_documentdb_the_scan(self, text: str) -> None:
        assert _penalties(_collector(text, source="postgresql"))["documentdb"] == self.PENALTY

    def test_a_traced_regex_is_read_without_an_approximation(self) -> None:
        (match,) = substring_matches(
            _collector("SELECT id FROM users WHERE email REGEXP 'x'")
        ).values()
        assert not match.word_search
        assert match.approximations == ()

    def test_a_wildcard_like_with_a_word_search_and_a_regex_is_still_scanned(self) -> None:
        text = "SELECT id FROM posts WHERE title LIKE '%term%' AND email REGEXP 'x'"
        assert _penalties(_collector(text))["aurora_mysql"] == self.PENALTY


class TestPrefixRegexFact:
    @pytest.mark.parametrize(
        ("text", "source", "expected"),
        [
            ("SELECT 1 FROM t WHERE a ~ '^abc'", "postgresql", True),
            ("SELECT 1 FROM t WHERE a ~* '^abc'", "postgresql", False),
            ("SELECT 1 FROM t WHERE a ~ $1", "postgresql", False),
            ("SELECT 1 FROM t WHERE a ~ '^(a|b)'", "postgresql", False),
            # Review of #1087: a top-level alternation anchors only its first branch.
            ("SELECT 1 FROM t WHERE a ~ '^a|b'", "postgresql", False),
            ("SELECT 1 FROM t WHERE a ~ '^ab|.*x'", "postgresql", False),
            # A quantifier that can match zero times leaves no fixed prefix.
            ("SELECT 1 FROM t WHERE a ~ '^a*'", "postgresql", False),
            ("SELECT 1 FROM t WHERE a ~ '^a?b'", "postgresql", False),
            ("SELECT 1 FROM t WHERE a ~ '^a{0,2}b'", "postgresql", False),
            # A negated match: no index serves "not this prefix".
            ("SELECT 1 FROM t WHERE a !~ '^abc'", "postgresql", False),
            ("SELECT 1 FROM t WHERE NOT (a ~ '^abc')", "postgresql", False),
            # Re-review of #1087: a NOT anywhere above the match, or an IS test that is not
            # IS TRUE, negates it.
            ("SELECT 1 FROM t WHERE NOT (s = 'x' AND a ~ '^abc')", "postgresql", False),
            ("SELECT 1 FROM t WHERE NOT (s = 'x' OR (b = 1 AND a ~ '^abc'))", "postgresql", False),
            ("SELECT 1 FROM t WHERE (a ~ '^abc') IS NOT TRUE", "postgresql", False),
            ("SELECT 1 FROM t WHERE (a ~ '^abc') IS FALSE", "postgresql", False),
            ("SELECT 1 FROM t WHERE (a ~ '^abc') IS TRUE", "postgresql", True),
            ("SELECT 1 FROM t WHERE s = 'x' AND a ~ '^abc'", "postgresql", True),
            # A NOT outside the subquery that holds the match does not negate its predicate.
            (
                "SELECT 1 FROM t WHERE NOT EXISTS (SELECT 1 FROM u WHERE u.a ~ '^abc')",
                "postgresql",
                True,
            ),
            # One required literal character is a prefix: the quantifier is after it.
            ("SELECT 1 FROM t WHERE a ~ '^ab*'", "postgresql", True),
            ("SELECT 1 FROM t WHERE a ~ '^a+'", "postgresql", True),
            ("SELECT 1 FROM t WHERE a ~ '^ab(c|d)'", "postgresql", True),
            # MySQL's REGEXP is case-insensitive on a nonbinary string.
            ("SELECT 1 FROM t WHERE a REGEXP '^abc'", "mysql", False),
            ("SELECT 1 FROM t WHERE a = 'x'", "postgresql", False),
        ],
    )
    def test_only_a_case_sensitive_anchored_literal(self, text, source, expected) -> None:
        assert query_facts(text, source).prefix_regex is expected

    def test_the_classifier_reads_the_fact_and_documentdb_scores_75(self) -> None:
        """The prefix_regex class fact comes from the classifier on a real query, not a
        hand-built fact set (review of #1087)."""
        from src.agents.referee.access_pattern import SchemaKeys, classify_query
        from src.shared.catalog.access_classes import access_classes, class_score

        collector = _collector("SELECT id FROM users WHERE email ~ '^admin'", source="postgresql")
        keys = SchemaKeys.from_collector(collector)
        by_id = {c.id: c for c in access_classes()}

        def documentdb_score(text: str) -> tuple[frozenset[str], int]:
            query = {**collector["queries"]["query_patterns"][0], "query_text": text}
            result = classify_query(query, keys, "postgresql")
            return result.facts, class_score(by_id[result.name], "documentdb", result.facts)

        facts, score = documentdb_score("SELECT id FROM users WHERE email ~ '^admin'")
        assert {"pattern_match", "prefix_regex"} <= facts
        assert score == 75
        facts, score = documentdb_score("SELECT id FROM users WHERE email ~ 'admin'")
        assert "prefix_regex" not in facts
        assert score == 45

    def test_documentdb_rates_a_prefix_regex_as_an_index_read(self) -> None:
        from src.shared.catalog.access_classes import class_engine_scores

        any_regex = class_engine_scores(frozenset({"pattern_match"}))["text_search"]
        prefix = class_engine_scores(frozenset({"pattern_match", "prefix_regex"}))["text_search"]
        assert (any_regex["documentdb"], prefix["documentdb"]) == (45, 75)
        # No other engine's rating moves: the fact describes DocumentDB's index only.
        assert {e: s for e, s in prefix.items() if e != "documentdb"} == {
            e: s for e, s in any_regex.items() if e != "documentdb"
        }


REGEX_JOIN = "SELECT * FROM orders o JOIN tags t ON t.id = o.id WHERE {} REGEXP 'x.y'"


class TestARegularExpressionIsSizedByTheTableOfItsColumn:
    """#1098: a regular expression's scan is sized by the table that holds the column it
    tests, as #942 does for a LIKE, not by the largest table the query reads."""

    def _match(self, text: str, **kwargs):
        return substring_matches(_schema_collector(text, tables=JOIN_TABLES, **kwargs))["q1"]

    def test_a_regex_on_the_small_table_of_a_join_does_not_scan_the_large_one(self) -> None:
        match = self._match(REGEX_JOIN.format("t.label"))
        assert (match.table_rows, match.scan_matters, match.approximations) == (SMALL, False, ())
        penalties, _ = scan_adjustments(
            _schema_collector(REGEX_JOIN.format("t.label"), tables=JOIN_TABLES)
        )
        assert penalties == {}

    def test_a_regex_on_the_large_table_is_a_scan_that_matters(self) -> None:
        match = self._match(REGEX_JOIN.format("o.note"))
        assert (match.table_rows, match.scan_matters, match.approximations) == (BIG, True, ())

    def test_an_unqualified_column_and_a_function_of_it_name_its_table(self) -> None:
        for column in ("label", "lower(t.label)"):
            match = self._match(REGEX_JOIN.format(column))
            assert (match.table_rows, match.scan_matters, match.approximations) == (
                SMALL,
                False,
                (),
            ), column

    def test_each_dialect_form_reads_its_column(self) -> None:
        forms = [
            (
                "SELECT * FROM orders o JOIN tags t ON t.id = o.id WHERE t.label ~ 'x.y'",
                "postgresql",
            ),
            (
                "SELECT * FROM orders o JOIN tags t ON t.id = o.id WHERE t.label ~* 'x.y'",
                "postgresql",
            ),
            (
                "SELECT * FROM orders o JOIN tags t ON t.id = o.id WHERE t.label SIMILAR TO 'x%'",
                "postgresql",
            ),
            (
                "SELECT * FROM orders o JOIN tags t ON t.id = o.id WHERE t.label RLIKE 'x.y'",
                "mysql",
            ),
            (
                "SELECT * FROM orders o JOIN tags t ON t.id = o.id WHERE REGEXP_LIKE(t.label, 'x')",
                "mysql",
            ),
            (
                "SELECT * FROM orders o JOIN tags t ON t.id = o.id WHERE t.label REGEXP 'x'",
                "mariadb",
            ),
            (
                "SELECT * FROM orders o JOIN tags t ON t.id = o.id WHERE t.label !~* 'x'",
                "postgresql",
            ),
            (
                "SELECT * FROM orders o JOIN tags t ON t.id = o.id "
                "WHERE REGEXP_LIKE(t.label, 'x', 'i')",
                "oracle",
            ),
            (
                "SELECT * FROM orders o JOIN tags t ON t.id = o.id WHERE REGEXP_LIKE(t.label, 'x')",
                "db2",
            ),
            (
                "SELECT * FROM orders o JOIN tags t ON t.id = o.id WHERE REGEXP_LIKE(t.label, 'x')",
                "sqlserver",
            ),
        ]
        for text, source in forms:
            match = self._match(text, source=source)
            assert (match.table_rows, match.approximations) == (SMALL, ()), text

    def test_an_ambiguous_column_keeps_the_largest_table_and_says_so(self) -> None:
        match = self._match(REGEX_JOIN.format("id"))
        assert (match.table_rows, match.scan_matters) == (BIG, True)
        (note,) = match.approximations
        assert "column id of a regular expression was not traced" in note
        assert "largest table the query reads counts" in note

    def test_an_expression_over_two_columns_keeps_the_largest_table(self) -> None:
        match = self._match(REGEX_JOIN.format("concat(o.note, t.label)"))
        assert (match.table_rows, match.scan_matters) == (BIG, True)
        assert "names no single column" in match.approximations[0]

    def test_a_column_on_the_pattern_side_keeps_the_largest_table_and_says_why(self) -> None:
        match = self._match(REGEX_JOIN.replace("{} REGEXP 'x.y'", "'abc' REGEXP t.label"))
        assert (match.table_rows, match.scan_matters) == (BIG, True)
        (note,) = match.approximations
        assert "pattern read from a column" in note and "names no single column" not in note

    def test_a_column_no_collected_table_holds_keeps_the_largest_table(self) -> None:
        text = (
            "WITH c AS (SELECT label FROM tags) SELECT * FROM orders o, c WHERE c.label REGEXP 'x'"
        )
        match = self._match(text)
        assert (match.table_rows, match.scan_matters) == (BIG, True)
        assert "was not traced" in match.approximations[0]

    def test_a_statement_the_parser_cannot_read_keeps_the_largest_table(self) -> None:
        text = "SELECT * FROM orders o JOIN tags t ON t.id = o.id WHERE t.label REGEXP 'x' AND ((("
        match = self._match(text)
        assert (match.table_rows, match.scan_matters) == (BIG, True)
        assert any("could not read" in n for n in match.approximations)

    def test_a_traced_table_with_no_row_count_is_not_sized(self) -> None:
        tables: list[tuple[str, int | None, list[tuple[str, str]]]] = [
            ("orders", BIG, [("id", "bigint")]),
            ("tags", None, [("id", "bigint"), ("label", "varchar(40)")]),
        ]
        collector = _schema_collector(REGEX_JOIN.format("t.label"), tables=tables)
        match = substring_matches(collector)["q1"]
        assert (match.table_rows, match.scan_matters) == (None, False)
        assert "table tags has no collected row count" in match.approximations[0]

    def test_each_regex_is_read_on_its_own_table(self) -> None:
        both = (
            "SELECT * FROM orders o JOIN tags t ON t.id = o.id "
            "WHERE t.label REGEXP 'x' AND o.note REGEXP 'y'"
        )
        match = self._match(both)
        assert (match.table_rows, match.scan_matters, match.approximations) == (BIG, True, ())

    def test_a_like_and_a_regex_are_each_read_on_their_own_table(self) -> None:
        text = (
            "SELECT * FROM orders o JOIN tags t ON t.id = o.id "
            "WHERE o.note LIKE '%word%' AND t.label REGEXP 'x'"
        )
        match = self._match(text)
        # The word search on the large table is not scanned; the regex scans the small one.
        assert (match.word_search, match.table_rows, match.scan_matters) == (False, SMALL, False)

    def test_the_trace_text_passes_the_customer_wording_guard(self) -> None:
        from src.tools.validation.customer_text_guard import scan_text

        for column in ("id", "concat(o.note, t.label)"):
            _, notes = scan_adjustments(
                _schema_collector(REGEX_JOIN.format(column), tables=JOIN_TABLES)
            )
            for note in notes.values():
                assert scan_text(note, strict_reason_codes=False) == [], note
