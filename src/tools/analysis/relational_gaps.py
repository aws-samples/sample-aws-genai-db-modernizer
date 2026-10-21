"""Version and feature gaps for the relational move (#471), deterministic, no model.

Wave 1 moves the relational workload to one Aurora engine: the source-compatible one
for MySQL, MariaDB and PostgreSQL, the chosen one for SQL Server, Oracle and DB2
(#381). :func:`detect_relational_gaps` lists what the collected source uses that the
target lacks or runs differently, from the collector's source engine and version and
the features in the collected schema and queries. Every rule is data in
``src/catalog/gaps/*.toml`` (:mod:`src.shared.catalog.gaps`); this module only applies
them:

- a ``version`` rule compares the source's major version line with the target's lines
  in ``src/catalog/aurora_versions.toml`` (homogeneous sources only);
- text rules (``calls``, ``words``, ``syntax``, ``statement_endings``) read every query and every view,
  procedure and trigger definition through the SQL facade's tokens, in the source
  engine's dialect (:func:`src.tools.sql.facade.sql_tokens`), so a literal, a quoted
  identifier or a comment never matches;
- schema rules read column data types, table storage engines and collations, and
  procedure languages; ``routines`` lists every procedure, function and trigger;
  ``always`` applies to every source of the rule's engines.

Each gap carries its rule's impact, plain reason and documentation, and the evidence:
the table ids, query ids and other objects (columns, views, routines, triggers) it was
found in. Synthesis computes it once into ``report.json``'s
``facts.relational_gaps`` (ADR-032); it never changes routing.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from src.agents.referee.table_resolution import TableNameResolver
from src.shared.catalog.gaps import (
    AURORA_TARGETS,
    GAP_IMPACTS,
    SOURCE_ENGINES,
    AuroraVersionLines,
    GapRule,
    aurora_version_lines,
    gap_rules,
    parse_version_line,
)
from src.tools.sql.facade import SqlTokens, sql_tokens

__all__ = ["detect_relational_gaps", "normalized_data_type"]


def normalized_data_type(data_type: Any) -> str:
    """A column type as the rules name it: lower case, no length or precision, single
    spaces (``NVARCHAR(50)`` is ``nvarchar``, ``TIMESTAMP(6) WITH TIME ZONE`` is
    ``timestamp with time zone``)."""
    text = re.sub(r"\([^)]*\)", " ", str(data_type or "").lower())
    return " ".join(text.split())


@dataclass(frozen=True)
class _Text:
    """One piece of collected SQL: a query or a view, routine or trigger definition."""

    kind: str  # query, view, procedure, function, trigger
    id: str
    tokens: SqlTokens
    tables: tuple[str, ...] = ()


@dataclass
class _Evidence:
    tables: set[str] = field(default_factory=set)
    queries: set[str] = field(default_factory=set)
    objects: set[tuple[str, str]] = field(default_factory=set)

    def add_text(self, text: _Text) -> None:
        if text.kind == "query":
            self.queries.add(text.id)
        else:
            self.objects.add((text.kind, text.id))
        self.tables.update(text.tables)

    def __bool__(self) -> bool:
        return bool(self.tables or self.queries or self.objects)


def _phrase(text: str) -> tuple[str, ...]:
    """A rule's ``syntax`` entry as tokens, split the way the facade splits SQL."""
    return sql_tokens(text, "").tokens


def _has_phrase(tokens: tuple[str, ...], phrase: tuple[str, ...]) -> bool:
    n = len(phrase)
    first = phrase[0]
    return any(
        tokens[i] == first and tokens[i : i + n] == phrase for i in range(len(tokens) - n + 1)
    )


def _ends_a_statement(tokens: tuple[str, ...], phrase: tuple[str, ...]) -> bool:
    n = len(phrase)
    return any(
        tokens[i : i + n] == phrase and (i + n == len(tokens) or tokens[i + n] == ";")
        for i in range(len(tokens) - n + 1)
    )


def _word_hit(tokens: tuple[str, ...], word: str, unless: frozenset[str]) -> bool:
    """``word`` appears, and (when ``unless`` names tokens) not directly before one."""
    if not unless:
        return word in tokens
    return any(
        tok == word and (i + 1 == len(tokens) or tokens[i + 1] not in unless)
        for i, tok in enumerate(tokens)
    )


def _extension_hit(rule: GapRule, text: _Text, user_routines: frozenset[str]) -> bool:
    """A call to one of the rule's extension functions, or ``CREATE EXTENSION <ext>``
    (see :class:`src.shared.catalog.gaps.GapMatch`)."""
    match = rule.match
    if not match.extension:
        return False
    ext = match.extension.upper()
    tokens = text.tokens.tokens
    if _has_phrase(tokens, ("CREATE", "EXTENSION", ext)) or _has_phrase(
        tokens, ("CREATE", "EXTENSION", "IF", "NOT", "EXISTS", ext)
    ):
        return True
    schemas = [s.upper() for s in match.extension_schemas]
    for name in (c.upper() for c in match.extension_calls):
        if name in text.tokens.unqualified_calls and name not in user_routines:
            return True
        if any(f"{schema}.{name}" in text.tokens.calls for schema in schemas):
            return True
    return False


def _user_routines(rule: GapRule, collector: Mapping[str, Any]) -> frozenset[str]:
    """The rule's extension function names the collection also defines as a user
    routine: one in a schema that is not the extension's, or not compiled code. An
    unqualified call to one of them is that user function, not the extension's."""
    match = rule.match
    names = {c.upper() for c in match.extension_calls}
    schemas = {s.lower() for s in match.extension_schemas}
    out: set[str] = set()
    for p in _as_list((collector.get("database_schema") or {}).get("procedures")):
        if not isinstance(p, Mapping):
            continue
        name = str(p.get("procedure_name") or p.get("procedure_id") or "").upper()
        if name not in names:
            continue
        schema = str(p.get("schema_name") or "").lower()
        language = str(p.get("language") or "").lower()
        if (schema and schema not in schemas) or language not in ("c", ""):
            out.add(name)
    return frozenset(out)


def _text_hit(
    rule: GapRule, text: _Text, words: frozenset[str], user_routines: frozenset[str]
) -> bool:
    match = rule.match
    if any(c.upper() in text.tokens.calls for c in match.calls):
        return True
    unless = frozenset(u.upper() for u in match.unless_followed_by)
    if any(
        w.upper() in words and _word_hit(text.tokens.tokens, w.upper(), unless) for w in match.words
    ):
        return True
    if _extension_hit(rule, text, user_routines):
        return True
    if any(_ends_a_statement(text.tokens.tokens, _phrase(s)) for s in match.statement_endings):
        return True
    return any(_has_phrase(text.tokens.tokens, _phrase(s)) for s in match.syntax)


def _reads_text(rule: GapRule) -> bool:
    m = rule.match
    return bool(m.calls or m.words or m.syntax or m.statement_endings or m.extension_calls)


def _routine_kind(procedure: Mapping[str, Any]) -> str:
    return (
        "function"
        if str(procedure.get("procedure_type") or "").upper() == "FUNCTION"
        else "procedure"
    )


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _collected_tables(names: Iterable[Any], resolver: TableNameResolver | None) -> tuple[str, ...]:
    """The collected table or view ids a query's ``tables_accessed`` names.

    The evidence names only tables the collection defines (#1205): the collector lists
    ``unknown`` for a statement whose tables it could not read (an ``EXEC``, an
    ``ALTER INDEX``), and a name the schema does not hold or holds twice resolves to
    nothing (a bare name collected under two schemas is ambiguous, so it is dropped
    too, while a schema-qualified one resolves), so such a query adds no table. It
    still counts as a query. A trigger's
    table goes through the same reading (the collector writes its bare name). With no
    collected schema there is nothing to resolve against: every name stays except the
    ``unknown`` placeholder.
    """
    out: set[str] = set()
    for name in names:
        if resolver is None:
            if str(name) != "unknown":
                out.add(str(name))
        elif (table_id := resolver.resolve(str(name))) is not None:
            out.add(table_id)
    return tuple(sorted(out))


def _texts(collector: Mapping[str, Any], engine: str) -> list[_Text]:
    """Every query and definition of the collection, tokenized once."""
    schema = collector.get("database_schema") or {}
    resolver = TableNameResolver.from_collector(collector)
    out: list[_Text] = []
    for q in _as_list((collector.get("queries") or {}).get("query_patterns")):
        if isinstance(q, Mapping) and q.get("query_id") and q.get("query_text"):
            tables = _collected_tables(_as_list(q.get("tables_accessed")), resolver)
            out.append(
                _Text("query", str(q["query_id"]), sql_tokens(str(q["query_text"]), engine), tables)
            )
    for v in _as_list(schema.get("views")):
        if isinstance(v, Mapping) and v.get("view_id") and v.get("definition"):
            out.append(_Text("view", str(v["view_id"]), sql_tokens(str(v["definition"]), engine)))
    for p in _as_list(schema.get("procedures")):
        if isinstance(p, Mapping) and p.get("procedure_id") and p.get("definition"):
            out.append(
                _Text(
                    _routine_kind(p),
                    str(p["procedure_id"]),
                    sql_tokens(str(p["definition"]), engine),
                )
            )
    for t in _as_list(schema.get("triggers")):
        if isinstance(t, Mapping) and t.get("trigger_id") and t.get("definition"):
            tables = _collected_tables([t["table_id"]] if t.get("table_id") else [], resolver)
            out.append(
                _Text(
                    "trigger",
                    str(t["trigger_id"]),
                    sql_tokens(str(t["definition"]), engine),
                    tables,
                )
            )
    return out


def _schema_evidence(
    rule: GapRule,
    collector: Mapping[str, Any],
    evidence: _Evidence,
    resolver: TableNameResolver | None,
) -> None:
    match = rule.match
    schema = collector.get("database_schema") or {}
    # Evidence from the schema itself: its table ids are collected ids, nothing to resolve.
    types = {t.lower() for t in match.data_types}
    engines = {e.lower() for e in match.storage_engines}
    languages = {lang.lower() for lang in match.routine_languages}
    for table in _as_list(schema.get("tables")):
        if not isinstance(table, Mapping) or not table.get("table_id"):
            continue
        table_id = str(table["table_id"])
        if types:
            for column in _as_list(table.get("columns")):
                if (
                    isinstance(column, Mapping)
                    and normalized_data_type(column.get("data_type")) in types
                ):
                    evidence.tables.add(table_id)
                    evidence.objects.add(("column", f"{table_id}.{column.get('column_name')}"))
        storage = str(table.get("storage_engine") or "").lower()
        if storage and storage in engines:
            evidence.tables.add(table_id)
        collation = str(table.get("collation") or "").lower()
        if collation and any(fnmatch.fnmatchcase(collation, g.lower()) for g in match.collations):
            evidence.tables.add(table_id)
    ext_names = {c.upper() for c in match.extension_calls}
    ext_schemas = {s.lower() for s in match.extension_schemas}
    for p in _as_list(schema.get("procedures")):
        if not isinstance(p, Mapping) or not p.get("procedure_id"):
            continue
        language = str(p.get("language") or "").lower()
        p_schema = str(p.get("schema_name") or "").lower()
        if (
            str(p.get("procedure_name") or "").upper() in ext_names
            and language == "c"
            and (not p_schema or p_schema in ext_schemas)
        ):
            # The extension's own compiled function is installed.
            evidence.objects.add((_routine_kind(p), str(p["procedure_id"])))
        if match.routines or (language and language in languages):
            evidence.objects.add((_routine_kind(p), str(p["procedure_id"])))
    if match.routines:
        for t in _as_list(schema.get("triggers")):
            if isinstance(t, Mapping) and t.get("trigger_id"):
                evidence.objects.add(("trigger", str(t["trigger_id"])))
                evidence.tables.update(
                    _collected_tables([t["table_id"]] if t.get("table_id") else [], resolver)
                )


def _line_text(line: tuple[int, ...] | None) -> str | None:
    return ".".join(str(n) for n in line) if line else None


def _version_hit(
    check: str, line: tuple[int, ...] | None, target_lines: Iterable[tuple[int, ...]]
) -> bool:
    lines = sorted(target_lines)
    if check == "unknown":
        return line is None
    if line is None or not lines:
        return False
    if check == "older_than_target":
        return line < lines[0]
    return line > lines[-1]


def detect_relational_gaps(
    collector: Mapping[str, Any] | None,
    target_engine: str | None,
    *,
    rules: Iterable[GapRule] | None = None,
    versions: Mapping[str, AuroraVersionLines] | None = None,
) -> dict[str, Any] | None:
    """The relational move's version and feature gaps (``RelationalGapFacts``), or None
    when there is no collection, no known source engine or no Aurora target.

    ``target_engine`` is wave 1's Aurora engine (``retained_engine_for``). ``rules`` and
    ``versions`` default to the catalog's.
    """
    if not isinstance(collector, Mapping) or target_engine not in AURORA_TARGETS:
        return None
    source = ((collector.get("metadata") or {}).get("source_database")) or {}
    engine = str(source.get("engine") or "").strip().lower()
    if engine == "postgres":
        engine = "postgresql"
    if engine == "mysql" and "mariadb" in str(source.get("version") or "").lower():
        # A MariaDB server collected as MySQL (version "10.6.12-MariaDB") is MariaDB.
        engine = "mariadb"
    if engine not in SOURCE_ENGINES:
        return None
    version = source.get("version")
    version = str(version) if version not in (None, "") else None
    all_versions = aurora_version_lines() if versions is None else versions
    entry = all_versions.get(str(target_engine))
    version_checked = entry is not None and engine in entry.source_engines
    target_lines = list(entry.lines) if entry is not None and version_checked else []
    line = parse_version_line(engine, version) if version_checked else None
    parsed_lines = [
        parsed for t in target_lines if (parsed := parse_version_line(engine, t)) is not None
    ]

    applicable = [
        r
        for r in (gap_rules() if rules is None else rules)
        if engine in r.source_engines and target_engine in r.targets
    ]
    needs_text = any(_reads_text(r) for r in applicable)
    texts = _texts(collector, engine) if needs_text else []
    word_sets = [frozenset(t.tokens.tokens) for t in texts]

    resolver = TableNameResolver.from_collector(collector)
    gaps: list[dict[str, Any]] = []
    for rule in applicable:
        evidence = _Evidence()
        match = rule.match
        if match.version is not None:
            if not (version_checked and _version_hit(match.version, line, parsed_lines)):
                continue
        elif not match.always:
            if _reads_text(rule):
                user_routines = _user_routines(rule, collector)
                for text, words in zip(texts, word_sets, strict=True):
                    if _text_hit(rule, text, words, user_routines):
                        evidence.add_text(text)
            _schema_evidence(rule, collector, evidence, resolver)
            if not evidence:
                continue
        gaps.append(
            {
                "rule_id": rule.id,
                "feature": rule.feature,
                "impact": rule.impact,
                "reason": rule.reason,
                "docs": rule.docs,
                "tables": sorted(evidence.tables),
                "queries": sorted(evidence.queries),
                "objects": [{"kind": k, "id": i} for k, i in sorted(evidence.objects)],
            }
        )
    gaps.sort(key=lambda g: (GAP_IMPACTS.index(g["impact"]), g["rule_id"]))
    return {
        "source_engine": engine,
        "source_version": version,
        "source_line": _line_text(line),
        "target_engine": target_engine,
        "target_lines": target_lines,
        "version_checked": version_checked,
        "rules_checked": len(applicable),
        # #1327: the rules that look at what the source uses, not at its version. A
        # conversion the catalog has none for (MySQL to Aurora PostgreSQL, #1330) is
        # not checked for feature gaps, which the wave note says.
        "feature_rules_checked": sum(1 for r in applicable if r.match.version is None),
        "gaps": gaps,
        "gaps_by_impact": {
            impact: sum(1 for g in gaps if g["impact"] == impact) for impact in GAP_IMPACTS
        },
    }
