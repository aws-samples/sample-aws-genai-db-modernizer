"""The detector registry of the adversarial corpus (#1411).

Every SQL detector in the declared fact modules (``FACT_MODULES`` in
``tests/unit/tools/sql/test_one_definition_per_fact.py``) and every public function
of the prose modules below is either registered here, with how to read its answer
and what it should answer on the corpus, or listed as not a detector, with why.
``test_adversarial_corpus.py`` fails on a function that is neither, so a new
detector has to declare its expectations before it lands.

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
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field

from src.agents.referee import (
    query_shape,
    synthesis_grounding,
    transaction_shape,
    utility_statements,
)
from src.agents.referee.table_resolution import SourceScope
from src.shared import prose_style, unsupported_pattern
from src.tools.sql import facade, regex_facts
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

# The blanking passes other detectors read through miss a dialect's own forms:
# scan_sql takes no dialect (MySQL # comments and backslash escapes, PostgreSQL
# dollar quotes), and strip_comments_and_literals neither nests PostgreSQL and SQL
# Server block comments nor blanks SQL Server bracket identifiers.
BUG_BLANKING_MISSES_DIALECT_FORMS = 1416

# PostgreSQL's dollar-quoted strings, which a blanking pass with no dialect misses.
_DOLLAR_QUOTES = ("dollar_quoted_string", "nested_dollar_quoted_tags")


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
}

# The text facts read the dialect's blanked text (``sql_spans.code_text``, #1415), whose
# grammar ends a block comment at its first ``*/``.
_CODE_TEXT_FACTS = (
    "found_rows",
    "exists_or_in_subquery",
    "in_list",
    "duplicate_key_upsert",
    "stored_routine_keyword",
    "array_operation",
    "on_conflict_or_returning",
    "negation_word",
    "time_function_keyword",
    "geospatial_keyword",
    "min_max_call",
    "status_equals_value",
    "is_null",
    "count_star_or_one",
    "session_key",
    "search_depth",
    "needs_aggregation",  # through found_rows
)
_FACT_BUGS: dict[str, dict[str, int]] = {
    name: _bug(BUG_BLANKING_MISSES_DIALECT_FORMS, "nested_block_comment")
    for name in _CODE_TEXT_FACTS
}
# Blank comments and literals, but not a nested block comment or a dialect's own forms.
for _name in ("upsert", "write_statement_text"):
    _FACT_BUGS[_name] = _bug(BUG_BLANKING_MISSES_DIALECT_FORMS, "nested_block_comment")
_FACT_BUGS["lateral_join"] = _bug(
    BUG_BLANKING_MISSES_DIALECT_FORMS, "nested_block_comment", dialects=("sqlserver",)
)
_FACT_BUGS["string_aggregate_call"] = _bug(
    BUG_BLANKING_MISSES_DIALECT_FORMS, "mysql_hash_comment", *_DOLLAR_QUOTES
)
_NESTED = _bug(BUG_BLANKING_MISSES_DIALECT_FORMS, "nested_block_comment")
# The capability gate reads needs_aggregation and the utility check, so a keyword in a
# nested block comment's tail makes a point read relational.
_CAPABILITY_BUGS = _NESTED


_FACADE = "src/tools/sql/facade.py"
_REGEX = "src/tools/sql/regex_facts.py"
_QUERY_FACTS = "src/tools/sql/query_facts.py"
_UNSUPPORTED = "src/shared/unsupported_pattern.py"
_SHAPE = "src/agents/referee/query_shape.py"
_TRANSACTION = "src/agents/referee/transaction_shape.py"
_UTILITY = "src/agents/referee/utility_statements.py"

SQL_DETECTORS: tuple[SqlDetector, ...] = (
    *(
        SqlDetector(f"QueryFacts.{name}", _fact(name), bugs=_FACT_BUGS.get(name, {}))
        for name in _public_facts()
    ),
    SqlDetector(f"{_FACADE}:sql_facts", _sql_facts_shape),
    SqlDetector(
        f"{_FACADE}:table_references",
        lambda text, dialect: facade.table_references(text, dialect).tables,
    ),
    SqlDetector(f"{_FACADE}:access_facts", _access_shape),
    SqlDetector(f"{_FACADE}:sql_tokens", _tokens),
    SqlDetector(
        f"{_REGEX}:scan_sql",
        lambda text, dialect: _words(regex_facts.scan_sql(text)),
        expect=_KEEPS_IDENTIFIER_NAMES,
        bugs=_bug(
            BUG_BLANKING_MISSES_DIALECT_FORMS,
            "backslash_escaped_quote",
            "mysql_hash_comment",
            *_DOLLAR_QUOTES,
        ),
    ),
    SqlDetector(
        f"{_UNSUPPORTED}:strip_comments_and_literals",
        lambda text, dialect: _words(
            unsupported_pattern.strip_comments_and_literals(text, dialect)
        ),
        # Documented to blank quoted identifiers too.
        expect={entry: () for entry in _KEEPS_IDENTIFIER_NAMES},
        bugs=_NESTED | _bug(BUG_BLANKING_MISSES_DIALECT_FORMS, "bracket_identifiers"),
    ),
    SqlDetector(
        f"{_UNSUPPORTED}:is_dedup_only_group_by",
        unsupported_pattern.is_dedup_only_group_by,
        bugs=_bug(BUG_BLANKING_MISSES_DIALECT_FORMS, "bracket_identifiers"),
    ),
    SqlDetector(
        f"{_UNSUPPORTED}:non_scalar_function_names",
        lambda text, dialect: sorted(unsupported_pattern.non_scalar_function_names(text, dialect)),
        bugs=_NESTED,
    ),
    SqlDetector(
        f"{_SHAPE}:readable_sql",
        lambda text, dialect: _words(query_shape.readable_sql(text, dialect)),
        expect=_KEEPS_IDENTIFIER_NAMES,
        bugs=_NESTED,
    ),
    SqlDetector(f"{_SHAPE}:skips_locked_rows", query_shape.skips_locked_rows, bugs=_NESTED),
    SqlDetector(f"{_SHAPE}:locking_clause", query_shape.locking_clause, bugs=_NESTED),
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
        bugs=_CAPABILITY_BUGS,
    ),
    SqlDetector(
        f"{_SHAPE}:is_single_table_crud",
        lambda text, dialect: query_shape.is_single_table_crud(
            _query(text, dialect), ["id"], None, SourceScope(engine=dialect)
        ),
        bugs=_CAPABILITY_BUGS,
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
        # information_schema in a nested block comment's tail.
        bugs=_NESTED,
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

# Public functions of the fact modules that are not a detector of their own, with why.
NOT_SQL_DETECTORS: dict[str, str] = {
    f"{_FACADE}:sqlglot_dialect": "maps an engine name to a dialect; reads no SQL",
    f"{_FACADE}:read_sql": "sql_facts, access_facts and table_references from one parse",
    f"{_FACADE}:query_readings": "sql_facts and access_facts from one parse",
    f"{_FACADE}:regex_answer": "table_references' regex fallback, reached through it",
    f"{_FACADE}:regex_reading": "read_sql's regex fallback, reached through sql_facts",
    f"{_QUERY_FACTS}:query_facts": "the cached QueryFacts; each fact is registered",
    f"{_QUERY_FACTS}:read_timestamp_column": "QueryFacts.timestamp_column's schema step",
    f"{_QUERY_FACTS}:is_timestamp_type": "reads a column's collected type, not SQL",
    f"{_QUERY_FACTS}:locate_column": "resolves a column to its table; reads no SQL",
    f"{_QUERY_FACTS}:timestamp_column_trace": "a trace over a collection, not a detector",
    f"{_QUERY_FACTS}:remember_stored_facts": "caches a stored reading",
    f"{_QUERY_FACTS}:remember_collected_facts": "caches a collection's stored readings",
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
    "regex_range_predicate",
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
    "src/shared/prose_style.py": "the plain-prose style list and its dash rewriter",
    "src/tools/validation/customer_text_guard.py": "the internal-wording guard",
    "src/agents/referee/synthesis_grounding.py": "the summary and risk checks",
    "src/agents/referee/synthesis_prose.py": "the schema-design prose seam",
}

_PROSE = "src/shared/prose_style.py"
_GUARD = "src/tools/validation/customer_text_guard.py"
_GROUNDING = "src/agents/referee/synthesis_grounding.py"

# The target the summary checks compare a summary with: Aurora PostgreSQL first,
# OpenSearch second, one high risk, ElastiCache serving no table.
_WAVES = [
    {"wave": 1, "engines": ["aurora_postgresql"]},
    {"wave": 2, "engines": ["opensearch"]},
]
_RISKS = {"risks": [{"severity": "high"}]}
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
)

NOT_PROSE_DETECTORS: dict[str, str] = {
    f"{_PROSE}:collector_identifiers": "reads a collection's table and column names",
    f"{_PROSE}:style_list_markdown": "renders the style list for a prompt",
    f"{_GUARD}:scan_json": "scan_text over each string of a document, registered",
    f"{_GUARD}:scan_json_style": "find_style_hits over each string of a document, registered",
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
