"""Each query fact has one definition, and every reader uses it (#893, ADR-031 Decision 8).

#491 moved every routing and analysis detector into ``src.tools.sql.query_facts``
unchanged, which showed the same fact detected several times with different
answers (DynamoDB's complex-aggregation check never matched ``COUNT(*)``). #893
made each one definition. These guards keep it that way:

- only the declared fact modules hold SQL detectors (a new module with its own
  ``GROUP BY`` regex fails, wherever it is);
- in ``QueryFacts``, each parsed fact is the facade's (``SqlFacts``) field, read by
  one fact, and each text detector belongs to one fact;
- no two facts agree on nearly every query of the samples and the probe
  statements below unless they are declared related, with the reason: a second
  definition of an idea is exactly a fact that agrees with the first almost
  everywhere;
- the hints that skip a parse never change an answer.
"""

from __future__ import annotations

import ast
import inspect
import itertools
import re
from functools import cached_property
from pathlib import Path

import pytest

from src.tools.database.offline_parser import detect_source_engine, parse_offline_collection
from src.tools.sql import query_facts as query_facts_module
from src.tools.sql.facade import SqlFacts, access_facts, sql_facts
from src.tools.sql.query_facts import QueryFacts, query_facts, read_timestamp_column
from src.tools.sql.regex_facts import IN_LIST_RE, regex_range_predicate
from tests.unit.tools.sql.table_refs_snapshot import SAMPLES, load_sample

_REPO = Path(__file__).resolve().parents[4]
_QUERY_FACTS = _REPO / "src" / "tools" / "sql" / "query_facts.py"

# ---------------------------------------------------------------------------
# Declared fact modules (ADR-031 Decision 8): the only places a SQL detector lives
# ---------------------------------------------------------------------------

FACT_MODULES: dict[str, str] = {
    "src/tools/sql/facade.py": "the parsed facts (SqlFacts, AccessFacts), read from the sqlglot tree",
    "src/tools/sql/regex_facts.py": "each parsed fact's regex fallback, for a statement sqlglot can't read",
    "src/tools/sql/query_facts.py": "the text facts, and every reader's access to the parsed ones",
    "src/shared/unsupported_pattern.py": (
        "the dedup-only GROUP BY proof (#336) behind the dedup_only_group_by fact; its scans run "
        "on comment- and literal-stripped text inside that proof"
    ),
    "src/agents/referee/query_shape.py": (
        "the query-shape facts: the locking clause (#838), skipped locked rows, the primary-key "
        "ordered row limit (#857) and the single-table access shape (ADR-031 Decision 14, which "
        "the access-pattern switch reads with each query's class, #728). Only those: its GROUP "
        "BY, ORDER BY, LIKE, SELECT and range readings come from regex_facts "
        "(QUERY_SHAPE_OWN_DETECTORS)"
    ),
    "src/agents/referee/transaction_shape.py": (
        "the write-transaction shape (#493): how many VALUES rows a write inserts, the source "
        "an INSERT ... SELECT copies, an array of key values, set-returning functions and a "
        "MERGE's source and ON condition. Only those: its SELECT, IN-list, EXISTS and FROM "
        "readings come from regex_facts (#930, TRANSACTION_SHAPE_OWN_DETECTORS)"
    ),
    "src/agents/referee/utility_statements.py": (
        "the utility-statement fact (#327, #737): DDL, session and transaction control, "
        "metadata reads"
    ),
}

# Modules with a SQL regex that is not a routing or analysis query fact.
OTHER_SQL_MODULES: dict[str, str] = {
    "src/agents/collector/mysql_collector.py": (
        "the collector's time-range field, before any fact is read; its has_text_search is the "
        "text_search fact the live reader sets (#929)"
    ),
    "src/tools/database/mysql_tools.py": (
        "a live collector's own parsing of its source's output (filter, sort and time-range "
        "columns); its has_text_search is the text_search fact (#929)"
    ),
    "src/tools/database/postgres_tools.py": (
        "a live collector's own parsing of its source's output (filter, sort and time-range "
        "columns); its has_text_search is the text_search fact (#929)"
    ),
    "src/tools/database/filter_columns.py": (
        "the MySQL and PostgreSQL collectors' filter-column reading of a WHERE clause (#1009)"
    ),
    "src/tools/database/oracle_tools.py": (
        "a live collector's own parsing of its source's output (filter, sort and time-range "
        "columns); its has_text_search is the text_search fact (#929)"
    ),
    "src/tools/database/sqlserver_tools.py": (
        "a live collector's own parsing of its source's output (filter, sort and time-range "
        "columns); its has_text_search is the text_search fact (#929)"
    ),
    "src/tools/aws/rds_tools.py": "a live collector's own parsing of its source's output",
    "src/tools/database/offline_parser.py": "the offline collector's digest and bulk-load reading",
    "src/tools/database/ddl_parser.py": "DDL, not a workload query",
    "src/tools/database/sql_cte.py": "the collectors' CTE-name reading for table references",
    "src/tools/schema/aurora_common/sql_safety.py": "the safety check on generated DDL",
    "src/agents/schema_design/dynamodb_key_reconcile.py": "key templates and column types",
    "src/graph/cypher_guard.py": "Cypher, not SQL",
    "src/graph/schema.py": "the graph store's own DDL, not a workload query",
    "src/tools/schema/aurora_common/delta_merge.py": "the generated Aurora DDL",
    "src/report/executive_report.py": (
        "the executive report keeps quoted SQL out of what it prints (#1020); it reads no "
        "query fact"
    ),
}

# What the module guard reads as a SQL detector (#893 review): a regex over a SQL
# keyword or a fact's keyword; an ``in`` test, string method or string collection
# (any name, any depth) holding a SQL fragment or a timestamp/session column name.
# A tripwire for a second copy, not a proof: a detector that spells its keywords
# some other way gets past it.
_SQL_KEYWORDS = (
    r"select|join|group\s+by|order\s+by|ilike|like|limit|offset|having|union|recursive|"
    r"regexp|rlike|tsvector|tsquery|distinct|group_concat|string_agg|array_agg|jsonb|"
    r"openjson|returning|lateral|pg_trgm|where"
)
_SQL_CALLS = (
    r"count|sum|avg|min|max|exists|match|against|over|top|group_concat|string_agg|"
    r"array_agg|json_\w+|jsonb_\w+|openjson|similarity|to_tsvector|to_tsquery|"
    r"plainto_tsquery|ts_rank|date_trunc|extract"
)
_B, _E = r"(?<![\w.-])", r"(?![\w-])"
_SQL_KEYWORD_RE = re.compile(rf"(?i){_B}(?:{_SQL_KEYWORDS}){_E}")
_SQL_FRAGMENT_RE = re.compile(
    rf"(?i){_B}(?:{_SQL_CALLS}){_E}\s*[(]"
    rf"|{_B}(?:like|ilike|limit|top|offset|between|in)\s+['\"%?$@:]"
    rf"|{_B}(?:group|order)\s+by{_E}|{_B}fetch\s+(?:first|next){_E}"
    rf"|{_B}not\s+(?:exists|in){_E}|on\s+duplicate\s+key|match\s+against|replace\s+into"
    r"|^\s+(?:select|join|where|from|in|on|limit|like|and|or)\s+$"
    r"|^(?:created_at|updated_at|deleted_at|expires?_at|event_time|log_time|ingested_at|"
    r"session_id|sess_id|last_\w+_at|\w+_date)$"
)
_STRING_METHODS = frozenset({"count", "find", "rfind", "index", "startswith", "endswith"})


def _strings(node: ast.AST) -> list[str]:
    try:
        value = ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError):
        return [ast.unparse(node)] if isinstance(node, ast.JoinedStr) else []
    if isinstance(value, str):
        return [value]
    if isinstance(value, tuple | list | set | frozenset):
        return [v for v in value if isinstance(v, str)]
    return []


def _regex_text(pattern: str) -> str:
    """A regex's text read as SQL: ``\\b`` dropped, ``\\s*`` a space, escapes undone."""
    text = re.sub(r"\\s[*+?]?", " ", pattern)
    text = re.sub(r"\\b", "", text)
    return re.sub(r"\\(.)", r"\1", text)


def _sql_detectors(tree: ast.Module) -> list[str]:
    """Every SQL detector in ``tree``, as ``"<form>:<line>"``."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args:
            func, first = node.func, node.args[0]
            if isinstance(func.value, ast.Name) and func.value.id == "re":
                if any(
                    _SQL_KEYWORD_RE.search(t) or _SQL_FRAGMENT_RE.search(t)
                    for t in map(_regex_text, _strings(first))
                ):
                    found.append(f"regex:{node.lineno}")
            elif (
                func.attr in _STRING_METHODS
                and isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and _SQL_FRAGMENT_RE.search(first.value)
            ):
                found.append(f"method:{node.lineno}")
        elif isinstance(node, ast.Compare) and any(
            isinstance(op, ast.In | ast.NotIn) for op in node.ops
        ):
            left = node.left
            if isinstance(left, ast.Constant) and isinstance(left.value, str):
                if _SQL_FRAGMENT_RE.search(left.value):
                    found.append(f"in:{node.lineno}")
        elif isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
            value = node.value
            texts = (
                [s for arg in value.args for s in _strings(arg)]
                if isinstance(value, ast.Call)
                else _strings(value)
            )
            if sum(1 for t in texts if _SQL_FRAGMENT_RE.search(t)) >= 2:
                found.append(f"collection:{node.lineno}")
    return found


def test_only_declared_modules_hold_sql_detectors():
    found = {
        path.relative_to(_REPO).as_posix()
        for path in sorted((_REPO / "src").rglob("*.py"))
        if "node_modules" not in path.parts
        and _sql_detectors(ast.parse(path.read_text(encoding="utf-8")))
    }
    declared = set(FACT_MODULES) | set(OTHER_SQL_MODULES)
    assert not set(FACT_MODULES) & set(OTHER_SQL_MODULES)
    assert not found - declared, (
        "A SQL detector outside the declared fact modules: read a query fact "
        "(src.tools.sql.query_facts) instead, or declare the module here with the reason. "
        f"{sorted(found - declared)}"
    )
    assert not declared - found, f"No SQL detector left: remove {sorted(declared - found)}"


# The detectors query_shape.py defines itself, each for one of its documented facts.
# GROUP BY, ORDER BY, a pattern-match predicate, a literal-prefix LIKE, the SELECT word
# and the range predicate are not among them: it imports those from regex_facts (#893, #933).
QUERY_SHAPE_OWN_DETECTORS: dict[str, str] = {
    "_DISTINCT_RE": "the shape check's DISTINCT",
    "_CTE_RE": "the shape check's CTE",
    "_WHERE_RE": "the WHERE clause the key predicate is read from",
    "_EQUALS_RE": "the key predicate's equality",
    "_NOT_BEFORE_RE": "a NOT before an IN list",
    "_LEFT_OPERAND_RE": "the operand before an operator",
    "_RIGHT_OPERAND_RE": "the operand after an operator",
    "_COLUMN_RE": "a column operand",
    "_ORDER_BY_ITEMS_RE": "the primary-key ordered row limit's sort items",
    "_SORT_ITEM_RE": "one sort item",
    "_TOP_RE": "the primary-key ordered row limit's TOP",
    "_OFFSET_RE": "its OFFSET",
    "_WHERE_WORD_RE": "a WHERE word",
    "_FOR_LOCKING_RE": "the locking clause (#838)",
    "_LOCK_IN_SHARE_MODE_RE": "MySQL's locking clause",
    "_TABLE_HINTS_RE": "SQL Server's locking hints",
    "_HINT_ARGUMENTS_RE": "a hint's arguments",
    "_HINT_WORD_RE": "a hint's name",
}


def test_query_shape_defines_only_its_documented_facts():
    tree = ast.parse((_REPO / "src/agents/referee/query_shape.py").read_text(encoding="utf-8"))
    compiled = {
        node.targets[0].id
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Call)
        and ast.unparse(node.value.func) == "re.compile"
    }
    assert compiled == set(QUERY_SHAPE_OWN_DETECTORS), (
        "query_shape.py defines a detector that is not one of its documented facts: read it "
        f"from src.tools.sql.regex_facts. {sorted(compiled ^ set(QUERY_SHAPE_OWN_DETECTORS))}"
    )
    imported = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "src.tools.sql.regex_facts"
        for alias in node.names
    }
    assert {
        "GROUP_BY_RE",
        "IN_LIST_RE",
        "ORDER_BY_RE",
        "PATTERN_PREDICATE_RE",
        "RANGE_PREDICATE_RE",
        "SELECT_WORD_RE",
    } <= imported


def _compiled_and_imported(path: str) -> tuple[set[str], set[str]]:
    """The regexes ``path`` compiles at module level, and the names it imports from
    ``src.tools.sql.regex_facts``."""
    tree = ast.parse((_REPO / path).read_text(encoding="utf-8"))
    compiled = {
        node.targets[0].id
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Call)
        and ast.unparse(node.value.func) == "re.compile"
    }
    imported = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "src.tools.sql.regex_facts"
        for alias in node.names
    }
    return compiled, imported


# The detectors transaction_shape.py defines itself, each a fact of the write's shape no
# query fact or access fact gives (#930 audit). Its SELECT, IN-list, EXISTS and FROM
# readings are the shared ones in regex_facts.
TRANSACTION_SHAPE_OWN_DETECTORS: dict[str, str] = {
    "_VALUES_RE": (
        "where an INSERT's VALUES rows start, to count them; the access facts' inserts_values "
        "says only whether there are any"
    ),
    "_KEY_ARRAY_RE": (
        "an array of key values (= ANY(...)), a set the statement does not bound; the access "
        "facts read it as one equality"
    ),
    "_MERGE_VALUES_RE": "a MERGE whose source is a VALUES list (USING (VALUES ...))",
    "_MERGE_ON_RE": "that MERGE's ON condition",
    "_RANGE_RE": (
        "a comparison in that ON condition that can match many target rows (<, >, <>, "
        "BETWEEN, LIKE): not the range predicate fact, which leaves out <> and LIKE"
    ),
    "_SET_RETURNING_RE": (
        "a set-returning function an INSERT ... SELECT copies rows from with no FROM; the "
        "access facts see a table function only as a FROM source"
    ),
}


def test_transaction_shape_defines_only_its_documented_facts():
    compiled, imported = _compiled_and_imported("src/agents/referee/transaction_shape.py")
    assert compiled == set(TRANSACTION_SHAPE_OWN_DETECTORS), (
        "transaction_shape.py defines a detector that is not one of its documented facts: "
        f"read it from src.tools.sql.regex_facts. {sorted(compiled ^ set(TRANSACTION_SHAPE_OWN_DETECTORS))}"
    )
    assert {"EXISTS_CONDITION_RE", "FROM_WORD_RE", "IN_LIST_RE", "SELECT_WORD_RE"} <= imported


def test_the_exists_condition_is_one_definition():
    """The ``[NOT] EXISTS (`` the transaction shape blanks is the one the
    ``exists_or_in_subquery`` fact reads (#930)."""
    compiled, imported = _compiled_and_imported("src/tools/sql/query_facts.py")
    assert "EXISTS_CONDITION_RE" in imported
    assert not any("EXISTS" in name for name in compiled)


def test_the_in_list_is_one_definition():
    """The ``in_list`` fact reads ``regex_facts.IN_LIST_RE``, as the key predicate and
    the transaction shape do (#1010): no second IN-list test in query_facts."""
    compiled, imported = _compiled_and_imported("src/tools/sql/query_facts.py")
    assert "IN_LIST_RE" in imported
    assert not any("IN_LIST" in name for name in compiled)
    assert not hasattr(query_facts_module, "IN_LIST_KEYWORDS")
    # Whatever it is named, no substring test: the fact's body reads IN_LIST_RE and
    # holds no string constant (a ' in (' fragment) or helper call besides it.
    tree = ast.parse(_QUERY_FACTS.read_text(encoding="utf-8"))
    method = next(
        node
        for cls in tree.body
        if isinstance(cls, ast.ClassDef) and cls.name == "QueryFacts"
        for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == "in_list"
    )
    body = method.body[1:] if ast.get_docstring(method) else method.body
    nodes = [n for stmt in body for n in ast.walk(stmt)]
    assert any(isinstance(n, ast.Name) and n.id == "IN_LIST_RE" for n in nodes)
    assert not any(isinstance(n, ast.Constant) and isinstance(n.value, str) for n in nodes)
    assert not any(isinstance(n, ast.Name) and n.id in {"_contains_any", "_has"} for n in nodes)
    assert not any(isinstance(n, ast.Attribute) and n.attr in {"_has", "_lower"} for n in nodes)


@pytest.mark.parametrize(
    "text",
    [
        "SELECT * FROM t WHERE id IN\n(\n SELECT x FROM u)",
        "SELECT * FROM t WHERE id IN\t(1)",
        "SELECT * FROM t WHERE id in(1)",
        "SELECT * FROM t WHERE id in (1)",
        "SELECT * FROM t WHERE (a)IN(1)",
        "IN (1)",
        "SELECT * FROM t WHERE id IN",
        "SELECT * FROM t WHERE margin (1)",
        "SELECT lookup_in (1)",
        "",
    ],
)
def test_the_in_list_fact_agrees_with_the_shared_regex(text):
    assert QueryFacts(text).in_list is bool(IN_LIST_RE.search(text))


@pytest.mark.parametrize(
    "source",
    [
        'import re\nX_RE = re.compile(r"\\bGROUP\\s+BY\\b")',
        'import re\ndef f(t):\n    return re.search("like ", t)',
        'AGG_KEYWORDS = ("count(", "having count(")',
        # The three copies the #931 review got past the first guard:
        'def f(t):\n    return "group by" in t.lower()',
        'TIMESTAMP_WORDS = ("created_at", "updated_at", "event_time")',
        'import re\n_TOP_RE = re.compile(r"\\btop\\s*\\(", re.I)',
        'def f(t):\n    words = ("order by", "limit ?")\n    return any(w in t for w in words)',
        'def f(t):\n    return t.lower().count(" join ") > 1',
    ],
)
def test_the_module_guard_finds_a_sql_detector(source):
    assert _sql_detectors(ast.parse(source))


@pytest.mark.parametrize(
    "source",
    [
        'import re\nX_RE = re.compile(r"\\bdynamo[\\s_-]?db\\b")',
        'LABELS = {"group": "Group"}',
        'import re\nX = re.compile(r"\\bnot\\s+(?:required|needed)\\b")',  # prose
        'FIELDS = ("rows_returned_avg", "execution_time_ms_avg")',
        'def f(p):\n    return "session-store" in p',
        'def f(p):\n    return p.endswith(".json")',
        'QUERY_TYPES = ("MERGE", "SELECT")',
        'PERCENTILES = ("min", "max")',
    ],
)
def test_the_module_guard_ignores_what_is_not_a_sql_detector(source):
    assert _sql_detectors(ast.parse(source)) == []


# ---------------------------------------------------------------------------
# Inside QueryFacts: one fact per parsed field, one fact per text detector
# ---------------------------------------------------------------------------


def _class_members(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "QueryFacts")
    return {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}


def _parsed_fields_read(member: ast.FunctionDef) -> set[str]:
    """``X`` for every ``self._parsed.X`` in ``member``."""
    return {
        node.attr
        for node in ast.walk(member)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "_parsed"
    }


# Facts that also read the collection's schema: a method taking its column types
# (#935). The corpus has no column types, so each reads its no-types answer here.
SCHEMA_FACTS: dict[str, str] = {
    "timestamp_column": (
        "a column whose collected type is a timestamp, its table resolved from the facade's "
        "columns; with no column types, a timestamp-named one"
    ),
}


def _public_facts() -> list[str]:
    return [
        name
        for name, value in vars(QueryFacts).items()
        if isinstance(value, cached_property | property) and not name.startswith("_")
    ] + list(SCHEMA_FACTS)


def _fact(facts: QueryFacts, name: str) -> object:
    """A fact's value; a schema fact's with no column types, as a bool."""
    value = getattr(facts, name)
    return bool(value(None)) if name in SCHEMA_FACTS else value


def test_every_schema_fact_is_declared():
    """A public method of ``QueryFacts`` that takes the collection's column types is a
    fact that reads the schema: declared above, so the guards below read it too."""
    methods = {
        name
        for name, value in vars(QueryFacts).items()
        if callable(value)
        and not name.startswith("_")
        and "column_types" in inspect.signature(value).parameters
    }
    assert methods == set(SCHEMA_FACTS)


def _parsed_fact_fields() -> dict[str, str]:
    """``{fact: SqlFacts field}`` for each fact that is a facade field."""
    members = _class_members(ast.parse(_QUERY_FACTS.read_text(encoding="utf-8")))
    out = {}
    for name in _public_facts():
        fields = _parsed_fields_read(members[name])
        assert len(fields) <= 1, f"{name} reads several facade fields: {fields}"
        if fields:
            out[name] = fields.pop()
    return out


# SqlFacts fields no QueryFacts fact reads, with why.
_UNREAD_SQL_FACTS = {
    "has_joins": "join_count > 0; the readers use join_count or the collector's own has_joins",
    "has_aggregation": "the aggregation fact is group_by or a positive aggregate_function_count",
    "has_subquery": "triage reads the collector's has_subqueries, this same fact stored",
    "parsed_by": "how the facts were read, not a fact",
    "fallback_reason": "how the facts were read, not a fact",
    "dialect": "how the facts were read, not a fact",
}


def test_each_facade_fact_is_read_by_one_query_fact():
    fields = _parsed_fact_fields()
    by_field: dict[str, list[str]] = {}
    for fact, field_name in fields.items():
        by_field.setdefault(field_name, []).append(fact)
    assert all(len(v) == 1 for v in by_field.values()), by_field
    assert set(by_field) | set(_UNREAD_SQL_FACTS) == set(SqlFacts.__dataclass_fields__)
    assert not set(by_field) & set(_UNREAD_SQL_FACTS)


def _module_detectors(tree: ast.Module) -> set[str]:
    return {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
        and target.id.endswith(("_RE", "_KEYWORDS", "_HINTS", "_PREFIXES", "_SUFFIXES"))
    }


def test_each_text_detector_belongs_to_one_fact():
    """A regex or keyword table in ``query_facts`` is read by exactly one fact (or the
    one helper function behind it), so no two facts share a definition."""
    tree = ast.parse(_QUERY_FACTS.read_text(encoding="utf-8"))
    owners: dict[str, set[str]] = {name: set() for name in _module_detectors(tree)}
    units: list[ast.AST] = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    units += list(_class_members(tree).values())
    for unit in units:
        for node in ast.walk(unit):
            if isinstance(node, ast.Name) and node.id in owners:
                owners[node.id].add(unit.name)  # type: ignore[attr-defined]
    shared = {name: sorted(units_) for name, units_ in owners.items() if len(units_) != 1}
    # A regex fragment composed into others at import time has no reader of its own.
    shared = {k: v for k, v in shared.items() if v}
    assert not shared, f"Detectors read by several facts (or none): {shared}"


# ---------------------------------------------------------------------------
# The corpus: every query of the samples, and a probe statement for each fact
# ---------------------------------------------------------------------------

PROBES: list[tuple[str, str]] = [
    ("SELECT * FROM t WHERE a LIKE '%x'", ""),
    ("SELECT * FROM t WHERE [Body] LIKE N'%x%'", "sqlserver"),
    ("SELECT * FROM t WHERE a LIKE CONCAT('%', ?, '%')", "mysql"),
    ("SELECT * FROM t WHERE a LIKE ?", "mysql"),
    ("SELECT * FROM t WHERE a ILIKE $1", "postgresql"),
    ("SELECT * FROM t WHERE a LIKE 'x%'", ""),
    ("SELECT * FROM t WHERE MATCH (a) AGAINST ('x')", "mysql"),
    ("SELECT * FROM t WHERE v @@ to_tsquery('x')", "postgresql"),
    ("SELECT * FROM t WHERE CONTAINS([Body], @q)", "sqlserver"),
    ("SELECT * FROM t WHERE a ~* 'x'", "postgresql"),
    ("SELECT * FROM t WHERE a REGEXP 'x'", "mysql"),
    ("SELECT similarity(a, $1) FROM t", "postgresql"),
    ("SELECT ROW_NUMBER() OVER (ORDER BY id) FROM t", "postgresql"),
    ("SELECT LAG(a, 1) OVER (ORDER BY a) FROM t", "postgresql"),
    ("SELECT NTILE(4) OVER (ORDER BY a) FROM t", "postgresql"),
    ("SELECT FIRST_VALUE(a) OVER (ORDER BY a) FROM t", "postgresql"),
    ("SELECT SUM(x) OVER (PARTITION BY y) FROM t", "postgresql"),
    ("WITH RECURSIVE r AS (SELECT 1) SELECT * FROM r", "postgresql"),
    (
        "WITH [T] AS (SELECT Id FROM N UNION ALL SELECT n.Id FROM N n JOIN [T] t ON 1=1) "
        "SELECT * FROM [T]",
        "sqlserver",
    ),
    ("SELECT COUNT(*) FROM t", "mysql"),
    ("SELECT a FROM t GROUP BY a", "postgresql"),
    ("SELECT a, b, COUNT(*) FROM t GROUP BY ROLLUP (a, b)", "postgresql"),
    ("SELECT a, COUNT(*) FROM t GROUP BY a WITH ROLLUP", "mysql"),
    ("SELECT a, b, count(id), max(id) FROM t GROUP BY a, b", "postgresql"),
    ("SELECT SQL_CALC_FOUND_ROWS * FROM t LIMIT 10", "mysql"),
    ("SELECT FOUND_ROWS()", "mysql"),
    ("SELECT * FROM g JOIN u ON lower(g.name) = u.name_lower", "postgresql"),
    ("INSERT INTO t SELECT * FROM u", "postgresql"),
    ("SELECT * FROM a JOIN b ON 1=1 JOIN c ON 1=1", "postgresql"),
    ("SELECT * FROM a, b, c WHERE a.id = b.id AND b.id = c.id", "postgresql"),
    ("SELECT * FROM t WHERE NOT EXISTS (SELECT 1 FROM u)", "postgresql"),
    ("SELECT * FROM t WHERE a IN (SELECT b FROM u)", "postgresql"),
    ("SELECT * FROM t WHERE id IN (1, 2)", "postgresql"),
    ("SELECT * FROM t LIMIT 10", "postgresql"),
    ("SELECT * FROM t ORDER BY score DESC LIMIT 10", "postgresql"),
    ("SELECT TOP (5) * FROM t ORDER BY a", "sqlserver"),
    ("SELECT JSON_EXTRACT(doc, '$.a') FROM t", "mysql"),
    ("SELECT doc->>'a' FROM t", "postgresql"),
    ("SELECT JSON_VALUE(doc, '$.a') FROM t", "sqlserver"),
    ("SELECT * FROM t WHERE doc @> '{\"a\": 1}'", "postgresql"),
    ("SELECT * FROM t WHERE id = ANY(ARRAY[1, 2])", "postgresql"),
    ("SELECT * FROM t JOIN LATERAL (SELECT 1) x ON true", "postgresql"),
    ("INSERT INTO t VALUES (1) ON CONFLICT DO NOTHING", "postgresql"),
    ("INSERT INTO t VALUES (1) ON DUPLICATE KEY UPDATE a = 1", "mysql"),
    ("UPDATE a JOIN b ON b.id = a.id SET a.x = ?", "mysql"),
    ("SELECT GROUP_CONCAT(a) FROM t", "mysql"),
    ("CALL refresh_stats()", "mysql"),
    ("SELECT * FROM t WHERE created_at > ? ORDER BY created_at", "mysql"),
    ("SELECT * FROM t WHERE a BETWEEN 1 AND 2 ORDER BY a", "mysql"),
    ("SELECT * FROM t WHERE event_time >= ?", "mysql"),
    ("SELECT date_trunc('day', ts) FROM t", "postgresql"),
    ("SELECT * FROM sessions WHERE token = ?", "mysql"),
    ("SELECT * FROM [UserSession] WHERE [SessionToken] = @t", "sqlserver"),
    ("SELECT * FROM t WHERE user_id = ?", "mysql"),
    ("SELECT ST_Distance(a, b) FROM t", "postgresql"),
    ("SELECT * FROM t WHERE status = ?", "mysql"),
    ("SELECT * FROM t WHERE a IS NULL", "mysql"),
    ("SELECT * FROM t WHERE a NOT IN (1)", "mysql"),
    ("SELECT (SELECT 1 FROM u WHERE u.id = t.uid) FROM t", "postgresql"),
    ("SELECT * FROM t WHERE v @@ plainto_tsquery($1)", "postgresql"),
    ("MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN DELETE", "sqlserver"),
    ("SELECT * FROM t FOR JSON PATH", "sqlserver"),
    ("WITH w AS (INSERT INTO t VALUES (1) RETURNING id) SELECT * FROM w", "postgresql"),
    ("/* job */ TRUNCATE t", "postgresql"),
]


@pytest.fixture(scope="module")
def corpus() -> list[tuple[str, str]]:
    """``(query text, source engine)`` for every sample query, and the probes."""
    items = list(PROBES)
    for name in SAMPLES:
        data = load_sample(name)
        engine, _port = detect_source_engine(data.get("metadata"))
        items += [(q["query_text"], engine) for q in parse_offline_collection(data)["queries"]]
    return items


def test_the_hints_that_skip_a_parse_never_change_a_fact(corpus):
    """Each parsed fact is the facade's field (and ``range_predicate`` the access facts'
    ranges, #933), whether or not its hint skipped the parse (``QueryFacts._has`` and
    ``_may_aggregate``)."""
    fields = _parsed_fact_fields()
    for text, engine in corpus:
        facts, parsed = query_facts(text, engine), sql_facts(text, engine)
        for fact, field_name in fields.items():
            expected = getattr(parsed, field_name)
            if fact in SCHEMA_FACTS:
                expected = bool(read_timestamp_column(expected, None))
            assert _fact(facts, fact) == expected, (fact, text[:80])
        assert facts.aggregation is parsed.has_aggregation, text[:80]
        access = access_facts(text, engine)
        ranges = (
            any(scope.ranges for scope in access.scopes)
            if access.parsed_by == "sqlglot"
            else regex_range_predicate(text)
        )
        assert facts.range_predicate is ranges, text[:80]


def _truths(corpus: list[tuple[str, str]]) -> dict[str, frozenset[int]]:
    return {
        name: frozenset(
            i for i, (text, engine) in enumerate(corpus) if _fact(query_facts(text, engine), name)
        )
        for name in _public_facts()
    }


def test_every_fact_is_true_somewhere(corpus):
    """A fact true on no sample query and no probe could hide a duplicate."""
    assert [name for name, truths in _truths(corpus).items() if not truths] == []


# Fact pairs that agree on most queries but are two facts, with why. A pair whose
# one fact reads the other (``self.<fact>``) is related by construction, not listed.
RELATED_FACTS: dict[frozenset[str], str] = {
    frozenset({"leading_wildcard_like", "wildcard_likes"}): (
        "wildcard_likes lists each leading-wildcard LIKE (column, word search) whose existence "
        "is leading_wildcard_like; the facade sets wildcard_leading to bool(wildcard_likes)"
    ),
    frozenset({"aggregate_function_count", "needs_aggregation"}): (
        "the aggregation capability adds windows and FOUND_ROWS, and drops a dedup GROUP BY"
    ),
    frozenset({"window_function", "ranking_window_function"}): (
        "ROW_NUMBER, RANK and DENSE_RANK are one kind of window function"
    ),
    frozenset({"write_statement_text", "write_targets"}): (
        "a write statement names the table it writes; write_statement_text answers whether a "
        "statement writes, write_targets which tables (#992), and it is empty for CALL or COPY"
    ),
    frozenset({"where_word", "keyed_after_where"}): "a keyed read is a WHERE with an equality",
}

# Two facts whose true sets overlap at least this much (Jaccard) are one idea
# unless RELATED_FACTS says why not. The audit's duplicates overlapped 0.6 to 1.0.
OVERLAP = 0.6


def _composed() -> set[frozenset[str]]:
    """Fact pairs where one fact's definition reads the other."""
    members = _class_members(ast.parse(_QUERY_FACTS.read_text(encoding="utf-8")))
    facts = set(_public_facts())
    pairs = set()
    for name in facts:
        for node in ast.walk(members[name]):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
                and node.attr in facts - {name}
            ):
                pairs.add(frozenset({name, node.attr}))
    return pairs


def _undeclared_overlaps(
    truths: dict[str, frozenset[int]], declared: set[frozenset[str]]
) -> list[tuple[str, str, float]]:
    found = []
    for a, b in itertools.combinations(sorted(truths), 2):
        union = truths[a] | truths[b]
        overlap = len(truths[a] & truths[b]) / len(union) if union else 0.0
        if overlap >= OVERLAP and frozenset({a, b}) not in declared:
            found.append((a, b, round(overlap, 2)))
    return found


def test_no_two_facts_agree_almost_everywhere(corpus):
    declared = set(RELATED_FACTS) | _composed()
    assert _undeclared_overlaps(_truths(corpus), declared) == [], (
        "Two facts agree on nearly every query: a second definition of one idea. Make the "
        "readers share one fact, or declare the pair in RELATED_FACTS with why it is two."
    )
    stale = {pair for pair in RELATED_FACTS if not pair <= set(_public_facts())}
    assert not stale, f"RELATED_FACTS names facts that no longer exist: {stale}"


def test_the_overlap_guard_catches_a_second_definition():
    group_by = frozenset({1, 2, 3, 4, 5})
    near_copy = frozenset({1, 2, 3, 4})  # the same idea, one quirk apart
    truths = {"group_by": group_by, "group_by_substring": near_copy, "other": frozenset({9})}
    assert _undeclared_overlaps(truths, set()) == [("group_by", "group_by_substring", 0.8)]
    assert _undeclared_overlaps(truths, {frozenset({"group_by", "group_by_substring"})}) == []
