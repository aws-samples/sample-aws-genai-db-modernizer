"""The leading-wildcard LIKE as an unindexed substring match (#493, option C).

One engine-neutral fact, read from the facts layer
(:func:`src.tools.sql.query_facts.wildcard_likes`): a query's ``LIKE`` predicates
whose literal pattern starts with ``%``. The catalog
(``src/catalog/patterns/substring-match.toml``) says what each engine does with
one: Aurora PostgreSQL indexes it with ``pg_trgm`` (an extension to create),
OpenSearch serves it natively, and Aurora MySQL and DocumentDB run it only as a
scan of every row. Lacking a substring index is the capability gap ADR-031
Decision 2 step 5 lets a penalty follow, so a ``scan`` engine loses the
pattern's penalty, but only when the scan matters: the query reaches both
``scan_threshold`` values (a frequent query on a large table). A word search
(``'%word%'`` on a text column) is never penalised; each engine's redesign for
it (FULLTEXT, ``$text``, ``pg_trgm``) is stated as guidance instead. A
parameterised pattern (``LIKE $1``) is not a leading wildcard here (#912).

A regular expression (the ``pattern_match`` fact: ``REGEXP``, ``~``, ``~*``, ``SIMILAR
TO``) is the same scan on an engine with no index for it, under the same rule and
threshold (#982). It is never a word search, and it is sized like a ``LIKE``: by the table
that holds the column it tests (the ``regex_matches`` fact, #1098), or, when that column
cannot be traced, by the largest table the query reads (and the trace says so). A ``scan`` engine whose
entry has ``prefix_regex_index`` (DocumentDB) loses nothing on a query whose only scan
is a prefix regular expression (the ``prefix_regex`` fact): an index serves it.

Each ``LIKE`` is read against the table that holds its column (#942): the facts layer
names the column and the tables it can belong to (through the query's aliases and
scopes), :func:`~src.tools.sql.query_facts.locate_column` picks the collected table
that holds it, and that table's row count and the column's collected type decide
whether the scan matters and whether a word pattern is on a text column. When the
column cannot be traced to one collected table (an expression over several columns, a
derived table, an ambiguous name, a table the collector did not describe), the older
approximation stands (the largest table the query reads; an unknown type is text) and
the decision trace says so (:attr:`SubstringMatch.approximations`).

Every reader takes its numbers and sentences from here: the scorer
(:func:`scan_adjustments`, merged into the anti-pattern map by
``ScoringContext.from_pipeline``), the decision trace (the notes it returns,
which the resolver appends to the assignment reason of the engine a query lands
on) and synthesis (:func:`substring_match_risks`, one risk per engine, which
every deliverable renders through the facts layer). Engine names come from
``display_engine``; the catalog's ``why`` and ``guidance`` are verb phrases
written without one.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass

from src.agents.referee.table_resolution import TableNameResolver
from src.shared.catalog.engines import CatalogError
from src.shared.catalog.patterns import EngineSupport, PatternEntry, ScanThreshold, pattern
from src.shared.engine_names import display_engine
from src.tools.sql.query_facts import (
    RegexMatch,
    WildcardLike,
    locate_column,
    query_facts,
    source_engine_of,
)
from src.tools.sql.refs import TableRef

__all__ = [
    "SubstringMatch",
    "engine_reason",
    "scan_adjustments",
    "scan_penalty",
    "substring_match_risks",
    "substring_matches",
]

# Column types a word search runs on; a column the collector did not describe counts as
# text, since a LIKE compares strings.
_TEXT_TYPE_WORDS = ("char", "text", "clob", "string")


@dataclass(frozen=True)
class SubstringMatch:
    """One query with a leading-wildcard ``LIKE``, and whether its scan matters.

    ``word_search``: every such predicate in it is a word search on a text column.
    ``table_rows``: the row count of the table the scanned column (of a ``LIKE`` or a
    regular expression) is in (the
    largest, when several are scanned); the largest table the query reads for a column
    that could not be traced to one; None when none is known.
    ``scan_matters``: not a word search, and both ``scan_threshold`` values reached.
    ``approximations``: why a predicate was read the older way (the column could not be
    traced to one collected table, or its type was not collected), one sentence each;
    empty when every predicate was read from its own table and column (#942).
    ``regex_only``: the only scanned predicates are regular expressions (#982), and
    ``prefix_regex``: each of them is a prefix one an index can serve.
    """

    query_id: str
    word_search: bool
    calls_per_second: float
    table_rows: int | None
    scan_matters: bool
    approximations: tuple[str, ...] = ()
    regex_only: bool = False
    prefix_regex: bool = False


@dataclass(frozen=True)
class _LikeReading:
    """One ``LIKE``: a word search on a text column, the rows of the table it scans."""

    word_search: bool
    rows: int | None
    approximations: tuple[str, ...] = ()


def _pattern() -> PatternEntry:
    return pattern("substring-match")


def pattern_id() -> str:
    """The catalog id of the pattern this module prices."""
    return _pattern().id


def _threshold() -> ScanThreshold:
    threshold = _pattern().scan_threshold
    if threshold is None:  # the loader requires one with scan entries
        raise CatalogError("substring-match.toml: scan_threshold is missing")
    return threshold


def _entries() -> dict[str, EngineSupport]:
    return dict(_pattern().engines)


def _tables_by_name(collector: Mapping) -> dict[str, Mapping]:
    tables = ((collector.get("database_schema") or {}).get("tables")) or []
    out: dict[str, Mapping] = {}
    for table in tables:
        if not isinstance(table, Mapping):
            continue
        for key in (table.get("table_id"), table.get("table_name")):
            if key:
                out.setdefault(str(key).lower(), table)
                out.setdefault(str(key).rsplit(".", 1)[-1].lower(), table)
    return out


def _query_tables(query: Mapping, tables: Mapping[str, Mapping]) -> list[Mapping]:
    found = []
    for name in query.get("tables_accessed") or []:
        key = str(name).lower()
        table = tables.get(key) or tables.get(key.rsplit(".", 1)[-1])
        if table is not None and table not in found:
            found.append(table)
    return found


def _is_text_column(column: str | None, tables: list[Mapping]) -> bool:
    """The older reading, for a column not traced to a table: the type of the first
    query table that has a column of that name, text when none is described."""
    if column is None:
        return True
    for table in tables:
        for col in table.get("columns") or []:
            if str(col.get("column_name") or "").lower() == column:
                return _is_text_type(col.get("data_type"))
    return True


def _is_text_type(data_type: object) -> bool:
    kind = str(data_type or "").lower()
    return not kind or any(word in kind for word in _TEXT_TYPE_WORDS)


def _rows(tables: list[Mapping]) -> int | None:
    counts = [r for t in tables if (r := _row_count(t)) is not None]
    return max(counts) if counts else None


def _row_count(table: Mapping) -> int | None:
    count = table.get("row_count")
    return int(count) if isinstance(count, int | float) and not isinstance(count, bool) else None


class _Schema:
    """The collected tables by id, and how a written table name finds its id."""

    def __init__(self, collector: Mapping) -> None:
        self.by_name = _tables_by_name(collector)
        self.resolver = TableNameResolver.from_collector(collector)
        self.by_id: dict[str, Mapping] = {}
        for table in ((collector.get("database_schema") or {}).get("tables")) or []:
            if isinstance(table, Mapping) and table.get("table_id"):
                self.by_id[str(table["table_id"])] = table

    def table_id(self, ref: TableRef) -> str | None:
        if self.resolver is None:
            return None
        return self.resolver.resolve(ref.written) or self.resolver.resolve(ref.name)

    def column(self, table_id: str, name: str) -> Mapping | None:
        columns: list[Mapping] = self.by_id.get(table_id, {}).get("columns") or []
        return next((c for c in columns if str(c.get("column_name") or "").lower() == name), None)


_LARGEST_TABLE = ", so the largest table the query reads counts"
_UNPARSED_REGEX_READING = (
    f"a regular expression in a statement the parser could not read names no column{_LARGEST_TABLE}"
)
_OLDER_READING = (
    ", so the largest table the query reads counts and a column type not found is read as text"
)


def _read_like(like: WildcardLike, schema: _Schema, query_tables: list[Mapping]) -> _LikeReading:
    """One predicate, from the table that holds its column; the older reading (the
    largest table the query reads, an unknown type is text) when it can't be traced."""
    word = like.word_search
    notes: list[str] = []
    table_id = None
    if like.use is None:
        notes.append(f"the tested expression names no single column{_OLDER_READING}")
    else:
        table_id, why = locate_column(
            like.use,
            schema.table_id,
            lambda t, name: schema.column(t, name) is not None,
        )
        if table_id is None:
            notes.append(
                f"column {like.use.name} was not traced to one collected table ({why})"
                f"{_OLDER_READING}"
            )
    if table_id is None:
        if word:
            word = _is_text_column(like.column, query_tables)
        return _LikeReading(word, _rows(query_tables), tuple(notes))
    rows, unsized = _table_rows(schema, table_id)
    notes.extend(unsized)
    if word and like.column is not None:
        column = schema.column(table_id, like.column)
        data_type = column.get("data_type") if column is not None else None
        word = _is_text_type(data_type)
        if not data_type:
            notes.append(f"column {like.column} has no collected type, so it is read as text")
    elif word:
        notes.append(
            f"the tested expression wraps column {like.use.name if like.use else '?'}, whose "
            "type is not the expression's, so it is read as text"
        )
    return _LikeReading(word, rows, tuple(notes))


def _table_rows(schema: _Schema, table_id: str) -> tuple[int | None, tuple[str, ...]]:
    """The collected row count of ``table_id``, and the note when there is none."""
    rows = _row_count(schema.by_id[table_id])
    if rows is not None:
        return rows, ()
    name = schema.by_id[table_id].get("table_name") or table_id
    return None, (
        f"table {name} has no collected row count, so the scan cannot be sized and is not "
        "penalised",
    )


def _read_regex(match: RegexMatch, schema: _Schema, query_tables: list[Mapping]) -> _LikeReading:
    """One regular expression, from the table that holds the column it tests (#1098);
    the largest table the query reads when it can't be traced. Never a word search."""
    if match.use is None:
        note = (
            f"a regular expression tests a constant against a pattern read from a column{_LARGEST_TABLE}"
            if match.column_pattern
            else f"a regular expression's tested expression names no single column{_LARGEST_TABLE}"
        )
        return _LikeReading(False, _rows(query_tables), (note,))
    table_id, why = locate_column(
        match.use,
        schema.table_id,
        lambda t, name: schema.column(t, name) is not None,
    )
    if table_id is None:
        note = (
            f"column {match.use.name} of a regular expression was not traced to one collected "
            f"table ({why}){_LARGEST_TABLE}"
        )
        return _LikeReading(False, _rows(query_tables), (note,))
    rows, unsized = _table_rows(schema, table_id)
    return _LikeReading(False, rows, unsized)


def substring_matches(collector: Mapping | None) -> dict[str, SubstringMatch]:
    """``query_id -> SubstringMatch`` for every query with a leading-wildcard ``LIKE`` or a
    regular expression."""
    collector = collector or {}
    threshold = _threshold()
    schema = _Schema(collector)
    tables = schema.by_name
    source_engine = source_engine_of(collector)
    out: dict[str, SubstringMatch] = {}
    for query in (collector.get("queries") or {}).get("query_patterns") or []:
        qid = str(query.get("query_id") or "")
        facts = query_facts(str(query.get("query_text") or ""), source_engine)
        likes = facts.wildcard_likes
        if not qid or not (likes or facts.pattern_match):
            continue
        query_tables = _query_tables(query, tables)
        readings = [_read_like(like, schema, query_tables) for like in likes]
        regex_only = facts.pattern_match and all(r.word_search for r in readings)
        if facts.regex_matches:
            readings.extend(_read_regex(m, schema, query_tables) for m in facts.regex_matches)
        elif facts.pattern_match:  # the regex fallback names no column
            readings.append(_LikeReading(False, _rows(query_tables), (_UNPARSED_REGEX_READING,)))
        word = all(r.word_search for r in readings)
        cps = float(query.get("calls_per_second") or 0.0)
        scanned = [r.rows for r in readings if not r.word_search]
        known = [r for r in (scanned or [r.rows for r in readings]) if r is not None]
        matters = cps >= threshold.min_calls_per_second and any(
            r is not None and r >= threshold.min_table_rows for r in scanned
        )
        notes = tuple(dict.fromkeys(n for r in readings for n in r.approximations))
        out[qid] = SubstringMatch(
            qid,
            word,
            cps,
            max(known) if known else None,
            matters,
            notes,
            regex_only=regex_only,
            prefix_regex=regex_only and facts.prefix_regex,
        )
    return out


def scan_penalty(match: SubstringMatch, engine: str) -> int:
    """The points ``engine`` loses on ``match``: the pattern's penalty for a scan engine
    whose scan matters, else 0."""
    support = _entries().get(engine)
    if support is None or support.kind != "scan" or not match.scan_matters:
        return 0
    if match.prefix_regex and support.prefix_regex_index:
        return 0
    return _pattern().penalty


def _threshold_text() -> str:
    t = _threshold()
    return f"{t.min_calls_per_second:g} call(s) per second on {t.min_table_rows:,} rows"


def _load_text(match: SubstringMatch) -> str:
    rows = f"{match.table_rows:,} rows" if match.table_rows is not None else "an unmeasured table"
    return f"{match.calls_per_second:g} call(s) per second on {rows}"


def engine_reason(engine: str, match: SubstringMatch) -> str | None:
    """Why ``engine`` serves ``match`` as it does, as one or two sentences; None for an
    engine the pattern says nothing useful about (DynamoDB is ruled out before scoring)."""
    support = _entries().get(engine)
    if support is None or support.kind not in ("native", "scan"):
        return None
    text = f"{display_engine(engine)} {support.why}"
    if match.approximations:
        text += " Read approximately: " + "; ".join(match.approximations) + "."
    if support.kind != "scan":
        return text
    if match.word_search:
        return f"{text} This is a word search, so it is not penalised; redesign: {support.guidance}"
    if match.prefix_regex and support.prefix_regex_index:
        return f"{text} This is a prefix regular expression, which an index serves: not penalised."
    penalty = scan_penalty(match, engine)
    if penalty:
        return (
            f"{text} It loses {penalty} points on this query: {_load_text(match)} reaches the "
            f"scan threshold of {_threshold_text()}."
        )
    return (
        f"{text} Not penalised: {_load_text(match)} is below the scan threshold of "
        f"{_threshold_text()}; the scan stays cheap only while the table stays small or the "
        "query rare."
    )


def scan_adjustments(
    collector: Mapping | None,
    engines: Iterable[str] | None = None,
    exempt: Collection[tuple[str, str]] = frozenset(),
) -> tuple[dict[tuple[str, str], int], dict[tuple[str, str], str]]:
    """``(query_id, engine) -> penalty`` and ``-> note`` for every leading-wildcard query.

    A penalty only for a scan engine whose scan matters, unless the pair is in ``exempt``
    (a scan a redesign removes, ADR-031 Decision 2 Amendment 13, #1231). A note for every
    engine the pattern describes (``engines``, when given, limits them to the candidates):
    its own reason, plus the reason each other candidate scores lower when one does.
    """
    entries = _entries()
    candidates = [e for e in (engines if engines is not None else entries) if e in entries]
    penalties: dict[tuple[str, str], int] = {}
    notes: dict[tuple[str, str], str] = {}
    for qid, match in substring_matches(collector).items():
        reasons = {e: r for e in candidates if (r := engine_reason(e, match))}
        lower = {
            e: p for e in candidates if (qid, e) not in exempt and (p := scan_penalty(match, e))
        }
        for engine, reason in reasons.items():
            if engine in lower:
                penalties[(qid, engine)] = lower[engine]
            others = [
                f"{display_engine(o)} scores {p} lower here: {display_engine(o)} "
                f"{entries[o].why}"
                for o, p in sorted(lower.items())
                if o != engine
            ]
            notes[(qid, engine)] = " ".join([reason, *others])
    return penalties, notes


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


def _excerpt(text: str, limit: int = 80) -> str:
    one_line = " ".join(text.replace("`", "").split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1].rstrip() + "…"


def substring_match_risks(
    collector: Mapping | None,
    query_engine: Mapping[str, str],
    candidates: Iterable[str],
) -> list[dict]:
    """One risk per final engine that serves leading-wildcard queries and has something
    to say about them (no risk for an engine that serves them natively with nothing to
    do, such as OpenSearch). ``query_engine``: each in-scope query's final engine.
    ``candidates``: the analysed engines, whose lower scores the risk also states.

    MEDIUM when a scan engine keeps a query whose scan matters, LOW otherwise. The
    description carries the engine's reason (``engine_reason``) and, for the queries
    another candidate scored lower, that engine's reason too.
    """
    collector = collector or {}
    entries = _entries()
    matches = substring_matches(collector)
    texts = {
        str(q.get("query_id")): str(q.get("query_text") or "")
        for q in (collector.get("queries") or {}).get("query_patterns") or []
        if q.get("query_id")
    }
    tables = {
        str(q.get("query_id")): sorted({str(t) for t in q.get("tables_accessed") or []})
        for q in (collector.get("queries") or {}).get("query_patterns") or []
        if q.get("query_id")
    }
    others = [c for c in candidates if entries.get(c) and entries[c].kind == "scan"]
    by_engine: dict[str, list[SubstringMatch]] = {}
    for qid, match in matches.items():
        engine = query_engine.get(qid)
        if engine:
            by_engine.setdefault(engine, []).append(match)
    risks: list[dict] = []
    for engine in sorted(by_engine):
        support = entries.get(engine)
        if support is None or support.kind not in ("native", "scan"):
            continue
        members = sorted(by_engine[engine], key=lambda m: m.query_id)
        words = [m for m in members if m.word_search]
        scans = [m for m in members if scan_penalty(m, engine)]
        if support.kind == "native" and not support.guidance:
            continue
        name = display_engine(engine)
        n = len(members)
        listed = "; ".join(_excerpt(texts.get(m.query_id, m.query_id)) for m in members[:3])
        if n > 3:
            listed += f"; and {n - 3} more"
        parts = [
            f"[{engine}] {n} {'query uses' if n == 1 else 'queries use'} a leading-wildcard "
            f"LIKE or a regular expression on {name}: {listed}. {name} {support.why}"
        ]
        if scans:
            parts.append(f"{len(scans)} of them reach the scan threshold of {_threshold_text()}.")
        if words:
            parts.append(f"{len(words)} of them are word searches.")
        for other in sorted(o for o in others if o != engine):
            lower = [m for m in members if scan_penalty(m, other)]
            if lower:
                parts.append(
                    f"{display_engine(other)} scored {scan_penalty(lower[0], other)} lower on "
                    f"{len(lower)} of them: {display_engine(other)} {entries[other].why}"
                )
        mitigation = []
        if support.kind == "native":
            mitigation.append(_sentence(support.guidance))
        elif words:
            mitigation.append(f"For the word searches, {support.guidance}")
        if support.kind == "scan" and len(words) < n:
            mitigation.append(
                "Keep the scan only while the table stays small and the query rare; "
                "otherwise serve the substring match from an engine with a substring index."
            )
        risks.append(
            {
                "risk_type": "PERFORMANCE_DEGRADATION",
                "severity": "MEDIUM" if scans else "LOW",
                "description": " ".join(parts),
                "affected_tables": sorted({t for m in members for t in tables.get(m.query_id, [])}),
                "mitigation": " ".join(mitigation),
                "query_ids": [m.query_id for m in members],
            }
        )
    return risks
