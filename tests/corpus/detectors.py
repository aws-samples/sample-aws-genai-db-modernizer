# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The detector registry of the adversarial corpus (#1411).

Every SQL detector in the declared fact modules (``FACT_MODULES`` in
``tests/unit/tools/sql/test_one_definition_per_fact.py``) and every public function
of the prose modules below is either registered here, with how to read its answer
and what it should answer on the corpus, or listed as not a detector, with why.
``test_adversarial_corpus.py`` fails on a function that is neither, so a new
detector has to declare its expectations before it lands.

The other modules that read SQL with a heuristic (``OTHER_SQL_MODULES``: the live
collectors' filter, sort and time-range parsing, the CTE-name reading, the
``query_type`` rule, literal redaction, the foreign-syntax check of a cut, the
executive report's quoted-SQL filter) register their heuristics here too (#1420),
each in the dialects it reads. Every function of such a module that runs a regex is
registered or in ``NOT_OTHER_SQL_HEURISTICS`` with why. A module whose heuristics read
no collected workload SQL (DDL, Cypher, generated DDL, a model-written access
pattern) is in ``NOT_CORPUS_MODULES`` with what it reads.

A registered detector's answer on a corpus entry is compared with:

- ``expect[<entry id>@<dialect>]`` or ``expect[<entry id>]`` when it has one (a
  declared answer, with the reason next to it);
- otherwise its answer on the entry's twin (the same statement or text with the
  hostile part made inert). A prose entry with no twin needs a declared answer.

A wrong answer this PR does not fix is listed in ``bugs`` with the number of the
issue that tracks it: the case runs as ``xfail(strict=True)``, so it turns red the
day the fix lands and the mark has to go.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from unittest.mock import patch

from src.agents.collector import mysql_collector
from src.agents.referee import (
    query_shape,
    synthesis_grounding,
    transaction_shape,
    utility_statements,
)
from src.agents.referee.table_resolution import SourceScope
from src.assessment import cut
from src.report import executive_report
from src.shared import prose_style, unsupported_pattern
from src.shared.sql_spans import replace_strings_and_comments
from src.tools.database import (
    live_sql,
    mysql_tools,
    oracle_tools,
    postgres_tools,
    sql_cte,
    sqlserver_tools,
)
from src.tools.database.filter_columns import where_columns
from src.tools.sql import facade, redact, regex_facts
from src.tools.sql.query_facts import QueryFacts
from src.tools.sql.query_type import statement_query_type
from src.tools.validation import customer_text_guard
from tests.corpus.adversarial_inputs import (
    ALL_DIALECTS,
    PROSE_ENTRIES,
    SQL_ENTRIES,
    hostile_keywords,
)
from tests.unit.tools.sql.test_one_definition_per_fact import SCHEMA_FACTS, _public_facts

# ---------------------------------------------------------------------------
# Tracked bugs: wrong answers on the corpus, filed and left for their own fix
# ---------------------------------------------------------------------------


def _bug(issue: int, *ids: str, dialects: tuple[str, ...] = ALL_DIALECTS) -> dict[str, int]:
    """``{"<id>@<dialect>": issue}`` for each listed entry, in each of its dialects
    that is in ``dialects``."""
    by_id = {e.id: e for e in SQL_ENTRIES}
    return {f"{i}@{d}": issue for i in ids for d in by_id[i].dialects if d in dialects}


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SqlDetector:
    name: str  # "<module path>:<function>" or "QueryFacts.<fact>"
    read: Callable[[str, str], object]  # (text, dialect) -> a comparable answer
    expect: Mapping[str, object] = field(default_factory=dict)
    bugs: Mapping[str, int] = field(default_factory=dict)
    # The dialects it reads: a live collector reads only its own engine's text.
    dialects: tuple[str, ...] = ALL_DIALECTS


@dataclass(frozen=True)
class ProseDetector:
    name: str  # "<module path>:<function>"
    read: Callable[[str], object]  # text -> a comparable answer
    expect: Mapping[str, object] = field(default_factory=dict)
    bugs: Mapping[str, int] = field(default_factory=dict)
    linear_bug: int | None = None  # the issue tracking a non-linear cost on long text


def _words(text: str) -> tuple[str, ...]:
    """The corpus's hostile keywords left in ``text``: what a blanking pass must
    remove from a literal or a comment (an identifier keeps its name)."""
    return tuple(sorted(set(re.findall(r"[A-Za-z_]+", text.upper())) & hostile_keywords()))


def _query(text: str, dialect: str) -> dict:
    """The query pattern a referee detector reads: its text, type and tables."""
    return {
        "query_text": text,
        "query_type": statement_query_type(text, dialect),
        "tables_accessed": [ref.name for ref in facade.table_references(text, dialect).tables],
    }


def _sql_facts_shape(text: str, dialect: str) -> dict:
    """Every ``SqlFacts`` field but how it was read; columns by count (an identifier
    entry's twin names other columns)."""
    facts = asdict(facade.sql_facts(text, dialect))
    for how in ("parsed_by", "fallback_reason", "dialect"):
        facts.pop(how)
    facts["columns"] = len(facts["columns"])
    return facts


def _access_shape(text: str, dialect: str) -> tuple:
    """The access facts' shape: the statement kind and, per scope, how many of each
    predicate and what it does, with no column names."""
    access = facade.access_facts(text, dialect)
    return (
        access.kind,
        access.inserts_values,
        access.windowed_aggregate,
        access.window_function,
        tuple(
            (
                len(s.sources),
                len(s.equalities),
                len(s.ranges),
                len(s.joins),
                s.other_filters,
                len(s.order_by),
                s.limit,
                s.has_where,
                s.group_by,
                s.aggregate,
                s.count_only,
                s.skip_locked,
                s.text_search,
                s.like_parameter,
                len(s.or_branches),
                len(s.null_tests),
                len(s.not_null_tests),
                len(s.correlated),
            )
            for s in access.scopes
        ),
    )


def _tokens(text: str, dialect: str) -> tuple:
    tokens = facade.sql_tokens(text, dialect)
    return _words(" ".join(tokens.tokens)), _words(" ".join(tokens.calls))


def _fact(name: str) -> Callable[[str, str], object]:
    if name in SCHEMA_FACTS:  # with no column types, as the fact guard reads it
        return lambda text, dialect: bool(getattr(QueryFacts(text, dialect), name)(None))
    return lambda text, dialect: getattr(QueryFacts(text, dialect), name)


# What a blanking pass keeps: an identifier's name (scan_sql reads ``[order]`` and
# ``"order"`` as the column ``order``), never a literal or a comment.
_IDENTIFIER_NAMES = ("BY", "COUNT", "GROUP", "JOIN", "LIMIT", "ORDER")
_KEEPS_IDENTIFIER_NAMES = {
    "backtick_identifiers": _IDENTIFIER_NAMES,
    "bracket_identifiers": _IDENTIFIER_NAMES,
    "double_quoted_identifiers": _IDENTIFIER_NAMES,
    # A keyword as a quoted name (#1478): the name is kept, the keyword facts read none.
    **dict.fromkeys(
        (
            "where_as_double_quoted_identifier",
            "where_as_backtick_identifier",
            "where_as_bracket_identifier",
        ),
        ("IN",),
    ),
    # A sort clause and an escaped quote in a quoted name (#1737): the name is kept.
    **dict.fromkeys(
        (
            "order_by_as_backtick_identifier",
            "order_by_as_double_quoted_identifier",
            "order_by_as_bracket_identifier",
        ),
        ("BY", "LIMIT", "ORDER"),
    ),
    **dict.fromkeys(
        (
            "escaped_backtick_in_identifier",
            "escaped_double_quote_in_identifier",
            "escaped_bracket_in_identifier",
        ),
        ("BY", "ORDER"),
    ),
}

# A bare name next to a keyword, quoted (#1547): ``status`` is a word in the twin, a quoted
# name in the entry, so the two passes that blank or skip quoted names read one word less.
_WITHOUT_STATUS = {
    "bare_name_clauses_double_quoted@postgresql": ("EXISTS", "RETURNING", "SET", "UPDATE"),
    "bare_name_clauses_double_quoted@oracle": ("EXISTS", "RETURNING", "SET", "UPDATE"),
    "bare_name_clauses_backtick@mysql": ("EXISTS", "SET", "UPDATE"),
    "bare_name_clauses_bracket@sqlserver": ("EXISTS", "SET", "UPDATE"),
}

# A PostgreSQL routine body is code (``sql_spans.code_text``, #1416): the fact in it counts,
# on the entry and its twin alike (the same body under another dollar tag).
_FACT_EXPECT: dict[str, dict[str, object]] = {
    "stored_routine_keyword": {"do_block_body": True, "function_body": True},
    "exists_or_in_subquery": {"function_body": True},
}


_FACADE = "src/tools/sql/facade.py"
_REGEX = "src/tools/sql/regex_facts.py"
_QUERY_FACTS = "src/tools/sql/query_facts.py"
_UNSUPPORTED = "src/shared/unsupported_pattern.py"
_SHAPE = "src/agents/referee/query_shape.py"
_TRANSACTION = "src/agents/referee/transaction_shape.py"
_UTILITY = "src/agents/referee/utility_statements.py"

SQL_DETECTORS: tuple[SqlDetector, ...] = (
    *(
        SqlDetector(
            f"QueryFacts.{name}",
            _fact(name),
            expect=_FACT_EXPECT.get(name, {}),
        )
        for name in _public_facts()
    ),
    SqlDetector(f"{_FACADE}:sql_facts", _sql_facts_shape),
    SqlDetector(
        f"{_FACADE}:table_references",
        lambda text, dialect: facade.table_references(text, dialect).tables,
    ),
    SqlDetector(f"{_FACADE}:access_facts", _access_shape),
    # #1875: what a view definition projects, and the columns a write names.
    SqlDetector(
        f"{_FACADE}:read_view_definition",
        # Its shape, not the names: a twin renames a hostile quoted name.
        lambda text, dialect: tuple(
            (c.source.written, c.name == c.column)
            for c in facade.read_view_definition(text, dialect).columns
        ),
    ),
    SqlDetector(f"{_FACADE}:written_columns", facade.written_columns),
    # #1936: the tables and unknown calls one routine-body statement names.
    SqlDetector(
        f"{_FACADE}:routine_statement_refs",
        lambda text, dialect: facade.routine_statement_refs(text, dialect),
    ),
    SqlDetector(
        f"{_FACADE}:sql_tokens",
        _tokens,
        expect={key: (words, ("EXISTS",)) for key, words in _WITHOUT_STATUS.items()},
    ),
    SqlDetector(
        f"{_REGEX}:scan_sql",
        lambda text, dialect: _words(regex_facts.scan_sql(text, dialect)),
        expect=_KEEPS_IDENTIFIER_NAMES,
    ),
    SqlDetector(
        f"{_UNSUPPORTED}:strip_comments_and_literals",
        lambda text, dialect: _words(
            unsupported_pattern.strip_comments_and_literals(text, dialect)
        ),
        # Documented to blank quoted identifiers too.
        expect={**{entry: () for entry in _KEEPS_IDENTIFIER_NAMES}, **_WITHOUT_STATUS},
    ),
    SqlDetector(
        f"{_UNSUPPORTED}:is_dedup_only_group_by",
        unsupported_pattern.is_dedup_only_group_by,
    ),
    SqlDetector(
        f"{_UNSUPPORTED}:non_scalar_function_names",
        lambda text, dialect: sorted(unsupported_pattern.non_scalar_function_names(text, dialect)),
    ),
    SqlDetector(
        f"{_SHAPE}:readable_sql",
        lambda text, dialect: _words(query_shape.readable_sql(text, dialect)),
        expect=_KEEPS_IDENTIFIER_NAMES,
    ),
    SqlDetector(f"{_SHAPE}:skips_locked_rows", query_shape.skips_locked_rows),
    SqlDetector(f"{_SHAPE}:locking_clause", query_shape.locking_clause),
    SqlDetector(
        f"{_SHAPE}:primary_key_ordered_limit",
        lambda text, dialect: query_shape.primary_key_ordered_limit(text, ["id"], dialect),
    ),
    SqlDetector(
        f"{_SHAPE}:is_primary_key_first_row_read",
        lambda text, dialect: query_shape.is_primary_key_first_row_read(
            text, ["id"], None, dialect
        ),
    ),
    SqlDetector(
        f"{_SHAPE}:single_table_access_shape",
        lambda text, dialect: query_shape.single_table_access_shape(
            _query(text, dialect), ["id"], None, SourceScope(engine=dialect)
        ),
    ),
    SqlDetector(
        f"{_SHAPE}:is_single_table_crud",
        lambda text, dialect: query_shape.is_single_table_crud(
            _query(text, dialect), ["id"], None, SourceScope(engine=dialect)
        ),
    ),
    SqlDetector(
        f"{_TRANSACTION}:transaction_shape",
        lambda text, dialect: transaction_shape.transaction_shape(
            _query(text, dialect), SourceScope(engine=dialect)
        ),
    ),
    SqlDetector(
        f"{_UTILITY}:is_utility_statement",
        lambda text, dialect: utility_statements.is_utility_statement(
            text, _query(text, dialect)["tables_accessed"], SourceScope(engine=dialect)
        ),
    ),
    SqlDetector(
        f"{_UTILITY}:is_procedure_internal_statement",
        lambda text, dialect: utility_statements.is_procedure_internal_statement(
            text, _query(text, dialect)["tables_accessed"], SourceScope(engine=dialect)
        ),
    ),
    SqlDetector(
        f"{_UTILITY}:procedure_internal_reason",
        lambda text, dialect: utility_statements.procedure_internal_reason(
            text, _query(text, dialect)["tables_accessed"], SourceScope(engine=dialect)
        ),
    ),
    SqlDetector(
        f"{_UTILITY}:is_sequence_fetch",
        lambda text, dialect: utility_statements.is_sequence_fetch(text),
    ),
    SqlDetector(
        f"{_UTILITY}:is_sequence_state_read",
        lambda text, dialect: utility_statements.is_sequence_state_read(text),
    ),
)

# ---------------------------------------------------------------------------
# The other SQL modules (#1420): heuristics outside the declared fact modules
# ---------------------------------------------------------------------------

_COLLECTORS = {
    "mysql": ("src/tools/database/mysql_tools.py", mysql_tools),
    "postgresql": ("src/tools/database/postgres_tools.py", postgres_tools),
    "sqlserver": ("src/tools/database/sqlserver_tools.py", sqlserver_tools),
    "oracle": ("src/tools/database/oracle_tools.py", oracle_tools),
}
# The identifier quote each collector passes to where_columns.
_WHERE_QUOTE = {"mysql": "`", "postgresql": '"'}

# A keyword as a quoted name: the name is the column the WHERE filters on (the twin
# names another column).
_FILTERS_ON_LIMIT: dict[str, object] = {
    "backtick_identifiers@mysql": ["limit"],
    "bracket_identifiers@sqlserver": ["limit"],
    "double_quoted_identifiers@postgresql": ["limit"],
    "double_quoted_identifiers@oracle": ["limit"],
}


# Each of these is a time range in the engines' forms (#1420): a date, time or timestamp
# literal after a comparison or BETWEEN, NOW() less an interval, one in a DO block's body.
_TIME_RANGE_ENTRIES = (
    "iso_t_timestamp",
    "utc_timestamp",
    "offset_timestamp",
    "zone_name_timestamp",
    "offset_and_zone_timestamp",
    "go_default_timestamp",
    "between_iso_timestamps",
    "between_dates",
    "now_minus_interval",
    "do_block_time_range",
)
_IS_TIME_RANGE: dict[str, object] = dict.fromkeys(_TIME_RANGE_ENTRIES, True)


def _leaked_words(redactor: Callable[[str, str], str]) -> Callable[[str, str], object]:
    """The corpus's hostile keywords left outside literals and comments once the text is
    redacted by ``redactor``: a literal read to the wrong end leaks its words into code,
    or a value."""

    def read(text: str, dialect: str) -> object:
        # Uncached: a cached short text would make the linear-cost check compare a
        # lookup with a redaction.
        with patch.object(redact, "_CACHE", redact._SizeBoundCache(max_chars=0)):
            redacted = redactor(text, dialect)
        return _words(
            replace_strings_and_comments(redacted, dialect, string=str, comment=lambda _s: " ")
        )

    return read


# A quoted name keeps its words, and with no setting collected a string ends where either
# reading of standard_conforming_strings ends it: the span either reading hides is redacted
# whole (privacy first), so 'C:\' AND c IS NULL ... is one value.
_REDACTION_EXPECT: dict[str, object] = {
    **_KEEPS_IDENTIFIER_NAMES,
    "backslash_ends_no_string@postgresql": (),
}


def _cte_drops(text: str, dialect: str) -> tuple[str, ...]:
    """The words ``drop_cte_references`` blanked: each ``FROM``/``JOIN`` of a CTE name."""
    kept = Counter(sql_cte.drop_cte_references(text, dialect).split())
    return tuple(sorted((Counter(text.split()) - kept).elements()))


# The executive report's quoted-SQL filter (#1020) cuts a sentence that lists statements
# back to the clause before its colon: a statement there, hostile or not, never prints.
_GAP_CLAUSE = "Schema design gap: these four queries have no access pattern"


def _report_line(text: str, dialect: str) -> str:
    return executive_report.without_table_names(f"{_GAP_CLAUSE}: {text}", None)


def _where_columns(text: str, dialect: str) -> list[str]:
    """``where_columns`` as the MySQL and PostgreSQL collectors call it."""
    columns = live_sql.column_text(text, dialect)
    return [columns.name(c) for c in where_columns(columns.text, _WHERE_QUOTE[dialect])]


def _text_only(read: Callable[[str], object]) -> Callable[[str, str], object]:
    """A collector's parser takes the text alone: it reads its own engine's dialect."""
    return lambda text, _dialect: read(text)


def _collector_detectors() -> tuple[SqlDetector, ...]:
    """Each live collector's filter, sort and time-range parsing, in its own dialect."""
    detectors: list[SqlDetector] = []
    for dialect, (path, module) in _COLLECTORS.items():
        own = (dialect,)
        detectors += [
            SqlDetector(
                f"{path}:_extract_filter_columns",
                _text_only(module._extract_filter_columns),
                expect={k: v for k, v in _FILTERS_ON_LIMIT.items() if k.endswith(f"@{dialect}")},
                dialects=own,
            ),
            SqlDetector(
                f"{path}:_extract_sort_columns",
                _text_only(module._extract_sort_columns),
                dialects=own,
            ),
            SqlDetector(
                f"{path}:_has_time_range_filter",
                _text_only(module._has_time_range_filter),
                expect=_IS_TIME_RANGE,
                dialects=own,
            ),
        ]
    return tuple(detectors)


OTHER_SQL_DETECTORS: tuple[SqlDetector, ...] = (
    *_collector_detectors(),
    SqlDetector(
        "src/agents/collector/mysql_collector.py:_detect_time_range",
        # Every collector builds its patterns with it, in its own engine's dialect (#1743).
        mysql_collector._detect_time_range,
        expect=_IS_TIME_RANGE,
    ),
    SqlDetector(
        "src/tools/database/filter_columns.py:where_columns",
        # Its callers pass column text: comments blanked, each literal a placeholder, each
        # quoted name one placeholder word read back as its name (#1737).
        _where_columns,
        expect={k: v for k, v in _FILTERS_ON_LIMIT.items() if k.endswith(("mysql", "postgresql"))},
        dialects=tuple(_WHERE_QUOTE),
    ),
    SqlDetector("src/tools/database/sql_cte.py:drop_cte_references", _cte_drops),
    SqlDetector("src/tools/sql/query_type.py:statement_query_type", statement_query_type),
    SqlDetector(
        "src/tools/sql/redact.py:redact_literals",
        _leaked_words(redact.redact_literals),
        expect=_REDACTION_EXPECT,
    ),
    SqlDetector(
        # Collected view, procedure and trigger bodies, through the same span reading.
        "src/tools/sql/redact.py:redact_definition",
        _leaked_words(redact.redact_definition),
        expect=_REDACTION_EXPECT,
    ),
    SqlDetector("src/assessment/cut.py:foreign_syntax", cut.foreign_syntax),
    SqlDetector(
        "src/report/executive_report.py:without_table_names",
        _report_line,
        expect={
            # An English-word verb with a bare one-letter name reads as prose (#1738), so
            # the twin prints; the entry is cut only because its comment holds SQL.
            "optimize_table_with_line_comment": _GAP_CLAUSE + ".",
            "describe_with_comment": _GAP_CLAUSE + ".",
        },
    ),
)

# The OTHER_SQL_MODULES with no detector in the corpus: what their heuristics read, none of
# it a collected workload query.
NOT_CORPUS_MODULES: dict[str, str] = {
    "src/shared/secret_scan.py": (
        "finds credentials in text; its one SQL pattern (a column note naming a SQL type) "
        "only allows a value, it reads no workload query"
    ),
    "src/tools/database/ddl_parser.py": "reads DDL, not a workload query",
    "src/tools/schema/aurora_common/sql_safety.py": "checks generated DDL, not a workload query",
    "src/agents/schema_design/dynamodb_key_reconcile.py": (
        "its conditional-write check reads a model-written access pattern's key condition "
        "and description, not SQL"
    ),
    "src/graph/cypher_guard.py": "reads Cypher, not SQL",
    "src/graph/catalog.py": "the graph query catalog's own Cypher, not SQL",
    "src/graph/schema.py": "the graph store's own DDL, not a workload query",
    "src/tools/schema/aurora_common/delta_merge.py": "reads the generated Aurora DDL",
    "src/tools/data_movement/dms_step.py": "checks the step 1 DDL file it renders, not a workload query",
    "src/tools/data_movement/dms_preflight.py": "reads parameter values such as sql_mode, not a workload query",
}

# The functions of a registered OTHER_SQL_MODULES module that run a regex but are not a
# heuristic of their own, with why.
NOT_OTHER_SQL_HEURISTICS: dict[str, str] = {
    "src/tools/database/mysql_tools.py:_version_gte": "compares version strings",
    "src/tools/database/mysql_tools.py:MySQLRemoteCollector._text_rows": (
        "decodes the client's batch escapes in a routine's text (#1652); detects nothing"
    ),
    "src/tools/database/filter_columns.py:_read_word": "a helper of where_columns, registered",
    "src/tools/database/sql_cte.py:_blank": "blanks a span drop_cte_references masked",
    "src/tools/database/sql_cte.py:_visible_ctes": "a helper of drop_cte_references, registered",
    "src/report/executive_report.py:printable": "maps glyphs the PDF font lacks",
    "src/report/executive_report.py:_md_markup": "renders Markdown emphasis as PDF markup",
    "src/report/executive_report.py:_names_pattern": (
        "builds without_table_names' identifier pattern, registered"
    ),
    "src/report/executive_report.py:_sentences": "a helper of without_table_names, registered",
    "src/report/executive_report.py:_date": "reads the report's date",
    "src/assessment/cut.py:_view_tables": "reads a collected view definition's tables",
    "src/assessment/cut.py:_bracket_name": "a helper of foreign_syntax, registered",
    "src/tools/sql/query_type.py:_opens_a_parenthesized_select": (
        "a helper of statement_query_type, registered"
    ),
}
_REDACT = "src/tools/sql/redact.py"
for _fn in (
    "_token",
    "_number_patterns",
    "_long_digit_runs",
    "_quoted_runs_redacted",
    "_unterminated",
    "_prefix_start",
    "_shape",
    "_track_parens",
    "_positions",
    "_number_edits",
):
    NOT_OTHER_SQL_HEURISTICS[f"{_REDACT}:{_fn}"] = "a step of redact_literals, registered"
for _fn in ("_Redactor.redact", "_Redactor._redact", "_Redactor._string", "_Redactor._comment"):
    NOT_OTHER_SQL_HEURISTICS[f"{_REDACT}:{_fn}"] = (
        "a step of redact_literals and redact_definition, registered"
    )
for _fn in ("_credentials_masked", "_scrubbed_once", "redact_comment"):
    NOT_OTHER_SQL_HEURISTICS[f"{_REDACT}:{_fn}"] = (
        "redacts a comment's text (quoted runs, credential shapes, e-mail addresses, long "
        "digit runs): a "
        "comment is prose to it, with no SQL span to read"
    )

# Public functions of the fact modules that are not a detector of their own, with why.
NOT_SQL_DETECTORS: dict[str, str] = {
    f"{_FACADE}:sqlglot_dialect": "maps an engine name to a dialect; reads no SQL",
    f"{_FACADE}:routine_tokens": (
        "the dialect's tokenizer over a routine's text (#1936): it returns every token, "
        "literals included, and detects no fact; the facts read from routine text are "
        "routine_statement_refs'"
    ),
    f"{_FACADE}:read_sql": "sql_facts, access_facts and table_references from one parse",
    f"{_FACADE}:query_readings": "sql_facts and access_facts from one parse",
    f"{_FACADE}:regex_answer": "table_references' regex fallback, reached through it",
    f"{_FACADE}:regex_reading": "read_sql's regex fallback, reached through sql_facts",
    f"{_FACADE}:temp_object_name": (
        "classifies one sqlglot table node (#1876); reached through sql_facts and "
        "table_references"
    ),
    f"{_FACADE}:resolve_view_text": (
        "rewrites a statement with the collector's views resolved (#1875); it detects no "
        "fact, and the text it returns is read by the registered facts"
    ),
    f"{_QUERY_FACTS}:query_facts": "the cached QueryFacts; each fact is registered",
    f"{_QUERY_FACTS}:read_timestamp_column": "QueryFacts.timestamp_column's schema step",
    f"{_QUERY_FACTS}:is_timestamp_type": "reads a column's collected type, not SQL",
    f"{_QUERY_FACTS}:is_text_type": "reads a column's collected type, not SQL",
    f"{_QUERY_FACTS}:locate_column": "resolves a column to its table; reads no SQL",
    f"{_QUERY_FACTS}:timestamp_column_trace": "a trace over a collection, not a detector",
    f"{_QUERY_FACTS}:remember_stored_facts": "caches a stored reading",
    f"{_QUERY_FACTS}:remember_collected_facts": "caches a collection's stored readings",
    f"{_QUERY_FACTS}:reserve_fact_caches": "sizes the facts caches to a collection; reads no SQL",
    f"{_QUERY_FACTS}:source_engine_of": "reads a collection's engine name",
    f"{_UNSUPPORTED}:unsupported_pattern_ids": "reads a schema design's pattern record",
    f"{_UNSUPPORTED}:unsupported_pattern_label": "reads a schema design's pattern record",
    f"{_UNSUPPORTED}:unsupported_pattern_text": "reads a schema design's pattern record",
    f"{_UNSUPPORTED}:unsupported_pattern_mitigation": "reads a schema design's pattern record",
    f"{_UTILITY}:has_sequence_reference": (
        "takes text its callers already blanked (is_utility_statement, the Aurora choice)"
    ),
}
_REGEX_FALLBACK_REASON = (
    "the raw-text fallback the facade reads when sqlglot cannot: documented to read "
    "comments and literals as SQL (module docstring), reached through sql_facts and QueryFacts"
)
for _fn in (
    "regex_has_joins",
    "regex_join_count",
    "regex_has_aggregation",
    "regex_has_subquery",
    "regex_wildcard_likes",
    "regex_wildcard_leading",
    "regex_group_by",
    "regex_aggregate_function_count",
    "regex_like_parameter",
    "regex_fulltext_search",
    "regex_pattern_match",
    "regex_similarity_match",
    "regex_window_function",
    "regex_ranking_window_function",
    "regex_offset_window_function",
    "regex_windowed_aggregate",
    "regex_grouping_sets",
    "regex_recursive_cte",
    "regex_order_by",
    "regex_row_limit",
    "regex_top_n",
    "regex_timestamp_column_names",
    "regex_timestamp_column",
    "regex_json_access",
    "regex_multi_table_write",
    "regex_write_targets",
    "regex_append_targets",
    "regex_range_predicate",
    "regex_temp_objects",
    "regex_routine_source",
):
    NOT_SQL_DETECTORS[f"{_REGEX}:{_fn}"] = _REGEX_FALLBACK_REASON
for _fn, _reader in (
    ("has_match_against", "fulltext_search and search_depth"),
    ("aggregate_function_names", "the single-table access shape"),
    ("output_into_targets", "write_targets"),
):
    NOT_SQL_DETECTORS[f"{_REGEX}:{_fn}"] = f"a helper of {_reader}, registered"

# ---------------------------------------------------------------------------
# Prose rewriters and guards
# ---------------------------------------------------------------------------

# The modules that rewrite or check customer prose, and what each holds.
PROSE_MODULES: dict[str, str] = {
    "src/shared/prose_style.py": "the plain-prose style list and its dash and confidence-figure rewriters",
    "src/tools/validation/customer_text_guard.py": "the internal-wording guard",
    "src/agents/referee/synthesis_grounding.py": "the summary and risk checks",
    "src/agents/referee/synthesis_prose.py": "the schema-design prose seam",
    "src/report/executive_report.py": "the quoted-SQL and identifier filter on printed prose (#1020)",
}

_PROSE = "src/shared/prose_style.py"
_GUARD = "src/tools/validation/customer_text_guard.py"
_GROUNDING = "src/agents/referee/synthesis_grounding.py"
_EXEC = "src/report/executive_report.py"

# The target the summary checks compare a summary with: Aurora PostgreSQL first,
# OpenSearch second, one high risk, ElastiCache serving no table.
_WAVES = [
    {"wave": 1, "engines": ["aurora_postgresql"]},
    {"wave": 2, "engines": ["opensearch"]},
]
_RISKS = {"risks": [{"severity": "high"}]}
# A homogeneous MySQL source whose wave 1 moves nothing (#1383).
_NO_MOVE_WAVES = [{"wave": 1, "no_move_reason": "no_aurora_move", "moves_from": ["mysql"]}]
_ENGINE_TABLES = {"aurora_postgresql": ["db.wp_postmeta"], "elasticache": ["db.wp_options"]}


def _hits(text: str) -> tuple:
    return tuple(sorted({(h.kind, h.term) for h in prose_style.find_style_hits(text)}))


def _findings(findings: list) -> tuple:
    return tuple(sorted({f.kind for f in findings}))


def _flagged(findings: list[dict]) -> bool:
    return any(f["high_confidence"] for f in findings)


PROSE_DETECTORS: tuple[ProseDetector, ...] = (
    ProseDetector(
        f"{_PROSE}:find_style_hits",
        _hits,
        expect={
            "negation": (("filler", "robust"),),
            "numeric_ranges": (),
            "identifier_colon": (),
            "quoted_engine_id": (),
            "risk_and_wave_claims": (),
            "negated_attribution": (),
        },
    ),
    ProseDetector(
        f"{_PROSE}:replace_dashes",
        lambda text: prose_style.replace_dashes(text) == text,
        expect={
            "negation": True,
            "numeric_ranges": True,
            "identifier_colon": True,
            "quoted_engine_id": True,
            "risk_and_wave_claims": True,
            "negated_attribution": True,
        },
    ),
    ProseDetector(
        f"{_PROSE}:replace_confidence_figures",
        lambda text: prose_style.replace_confidence_figures(text) == text,
        expect={
            "negation": True,
            "numeric_ranges": True,
            "identifier_colon": True,
            "quoted_engine_id": True,
            "risk_and_wave_claims": True,
            "negated_attribution": True,
        },
    ),
    ProseDetector(
        f"{_PROSE}:has_confidence_figure",
        prose_style.has_confidence_figure,
        expect={
            "negation": False,
            "numeric_ranges": False,
            "identifier_colon": False,
            "quoted_engine_id": False,
            "risk_and_wave_claims": False,
            "negated_attribution": False,
        },
    ),
    # #2184: the design split's own terms; no corpus entry names one, so none changes.
    ProseDetector(
        f"{_PROSE}:replace_pipeline_terms",
        lambda text: prose_style.replace_pipeline_terms(text) == text,
        expect={
            "negation": True,
            "numeric_ranges": True,
            "identifier_colon": True,
            "quoted_engine_id": True,
            "risk_and_wave_claims": True,
            "negated_attribution": True,
        },
    ),
    ProseDetector(
        f"{_PROSE}:pipeline_terms_in",
        lambda text: tuple(prose_style.pipeline_terms_in(text)),
        expect={
            "negation": (),
            "numeric_ranges": (),
            "identifier_colon": (),
            "quoted_engine_id": (),
            "risk_and_wave_claims": (),
            "negated_attribution": (),
        },
    ),
    ProseDetector(
        f"{_PROSE}:pipeline_term_spans",
        lambda text: tuple(prose_style.pipeline_term_spans(text)),
        expect={
            "negation": (),
            "numeric_ranges": (),
            "identifier_colon": (),
            "quoted_engine_id": (),
            "risk_and_wave_claims": (),
            "negated_attribution": (),
        },
    ),
    ProseDetector(
        f"{_PROSE}:clean_model_prose",
        lambda text: prose_style.clean_model_prose(text, "fallback").accepted,
        expect={
            "negation": False,
            "numeric_ranges": True,
            "identifier_colon": True,
            "quoted_engine_id": True,
            "risk_and_wave_claims": True,
            "negated_attribution": True,
        },
    ),
    ProseDetector(
        f"{_GUARD}:scan_text",
        lambda text: _findings(customer_text_guard.scan_text(text)),
        expect={
            # A raw engine id is a leak even quoted (the module's _CODE_SPAN_RE note).
            "quoted_engine_id": ("engine_id",),
            "fenced_code_block": ("engine_id",),
            "negation": (),
            "numeric_ranges": (),
            "identifier_colon": (),
            "risk_and_wave_claims": (),
            "negated_attribution": (),
        },
    ),
    ProseDetector(
        f"{_GUARD}:scan_markdown",
        lambda text: _findings(customer_text_guard.scan_markdown(text)),
        expect={
            "quoted_engine_id": ("engine_id",),
            "fenced_code_block": ("engine_id",),
            "negation": (),
            "numeric_ranges": (),
            "identifier_colon": (),
            "risk_and_wave_claims": (),
            "negated_attribution": (),
        },
    ),
    ProseDetector(
        f"{_GROUNDING}:recommends_engine",
        lambda text: synthesis_grounding.recommends_engine(text, "opensearch"),
        expect={
            "negation": False,
            "numeric_ranges": False,
            "identifier_colon": False,
            "quoted_engine_id": False,
            "risk_and_wave_claims": True,  # "Move OpenSearch first"
            "negated_attribution": False,
        },
    ),
    ProseDetector(
        f"{_GROUNDING}:check_summary_grounding",
        lambda text: _flagged(
            synthesis_grounding.check_summary_grounding(text, _ENGINE_TABLES, "db")
        ),
        expect={
            "negation": False,
            "numeric_ranges": False,
            "identifier_colon": False,
            "quoted_engine_id": False,
            "risk_and_wave_claims": False,
            "negated_attribution": False,
        },
    ),
    ProseDetector(
        f"{_GROUNDING}:check_summary_internal_leaks",
        lambda text: bool(synthesis_grounding.check_summary_internal_leaks(text)),
        expect={
            "negation": False,
            "numeric_ranges": False,
            "identifier_colon": False,
            "quoted_engine_id": False,
            "risk_and_wave_claims": False,
            "negated_attribution": False,
        },
    ),
    ProseDetector(
        f"{_GROUNDING}:check_summary_style",
        lambda text: bool(synthesis_grounding.check_summary_style(text)),
        expect={
            "negation": True,
            "numeric_ranges": False,
            "identifier_colon": False,
            "quoted_engine_id": False,
            "risk_and_wave_claims": False,
            "negated_attribution": False,
        },
    ),
    ProseDetector(
        f"{_GROUNDING}:check_summary_wave_order",
        lambda text: _flagged(synthesis_grounding.check_summary_wave_order(text, _WAVES)),
        expect={
            "negation": False,
            "numeric_ranges": False,
            "identifier_colon": False,
            "quoted_engine_id": False,
            # "OpenSearch first, then Aurora PostgreSQL" against waves that move
            # Aurora PostgreSQL first.
            "risk_and_wave_claims": True,
            "negated_attribution": False,
        },
    ),
    ProseDetector(
        f"{_GROUNDING}:check_summary_risk_counts",
        lambda text: _flagged(synthesis_grounding.check_summary_risk_counts(text, _RISKS)),
        expect={
            "negation": False,
            "numeric_ranges": False,
            "identifier_colon": False,
            "quoted_engine_id": False,
            # "There are 9 high risks." against one high risk.
            "risk_and_wave_claims": True,
            "negated_attribution": False,
        },
    ),
    ProseDetector(
        f"{_GROUNDING}:check_summary_no_move_wave",
        lambda text: _flagged(synthesis_grounding.check_summary_no_move_wave(text, _NO_MOVE_WAVES)),
        expect={
            "negation": False,
            "numeric_ranges": False,
            "identifier_colon": False,
            "quoted_engine_id": False,
            "risk_and_wave_claims": False,
            "negated_attribution": False,
        },
    ),
    ProseDetector(
        f"{_EXEC}:without_table_names",
        lambda text: executive_report.without_table_names(text, None),
        expect={
            # Plain prose prints as written (its whitespace folded).
            **{
                entry.id: " ".join(entry.text.split())
                for entry in PROSE_ENTRIES
                if entry.id
                in ("negation", "numeric_ranges", "negated_attribution", "risk_and_wave_claims")
            },
            # A tilde fence holds no SQL and no identifier: nothing to cut.
            "tilde_fenced_code_block": (
                "The plan keeps reads local. ~~~ leverage — robust -- seamless ~~~"
            ),
            # A backtick quotes SQL, and a snake_case word is an identifier: the
            # sentence goes.
            "code_span": "",
            "identifier_colon": "",
            "quoted_engine_id": "",
        },
    ),
)

NOT_PROSE_DETECTORS: dict[str, str] = {
    f"{_PROSE}:collector_identifiers": "reads a collection's table and column names",
    f"{_PROSE}:style_list_markdown": "renders the style list for a prompt",
    f"{_GUARD}:scan_json": "scan_text over each string of a document, registered",
    f"{_GUARD}:scan_json_style": "find_style_hits over each string of a document, registered",
    f"{_GUARD}:is_id_field": "reads a field name and its parent key, not prose",
    f"{_GUARD}:undeclared_engine_fields": "reads a document's field names, not its prose",
    f"{_GROUNDING}:display_name": "maps an engine id to its display name",
    f"{_GROUNDING}:engine_mentions": "a helper of recommends_engine and ground_risks",
    f"{_GROUNDING}:split_sentences": "a helper of every summary check, registered",
    f"{_GROUNDING}:reality_check_settled_engines": "reads the reality check's output",
    f"{_GROUNDING}:eliminated_engines": "reads the reality check's output",
    f"{_GROUNDING}:never_justified_engines": "reads the reality check's output",
    f"{_GROUNDING}:ground_risks": "recommends_engine over a risk register, registered",
    f"{_GROUNDING}:recompute_reality_check_patterns": "reads the reality check's output",
    f"{_GROUNDING}:engine_table_scope": "reads the assignment, not prose",
    f"{_GROUNDING}:build_effective_architecture": "reads the assignment, not prose",
    f"{_GROUNDING}:build_fallback_summary": "writes deterministic text from the assignment",
    f"{_EXEC}:register_fonts": "registers the PDF fonts",
    f"{_EXEC}:font_for": "names a registered font",
    f"{_EXEC}:printable": "maps glyphs the font lacks; cuts nothing",
    f"{_EXEC}:table_names": "reads a report's table names, not prose",
    f"{_EXEC}:collected_identifiers": "reads a collection's identifiers, not prose",
    f"{_EXEC}:contrast_ratio": "compares two colours",
    f"{_EXEC}:card_mitigation": "without_table_names over a risk's mitigation, registered",
    f"{_EXEC}:build_story": "lays out the PDF from a report",
    f"{_EXEC}:section_headings": "names the report's pages, not prose (#1133)",
    f"{_EXEC}:render_executive_report_pdf": "writes the PDF",
    "src/agents/referee/synthesis_prose.py:clean_schema_design_prose": (
        "clean_model_prose over each model-written field of a schema design, registered"
    ),
}


def entry_keys() -> tuple[set[str], set[str]]:
    """Every SQL ``<id>@<dialect>`` key and every prose entry id."""
    return (
        {f"{e.id}@{d}" for e in SQL_ENTRIES for d in e.dialects},
        {e.id for e in PROSE_ENTRIES},
    )
