# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

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
- text rules (``calls``, ``words``, ``syntax``, ``statement_endings``,
  ``statement_starts``, ``word_prefixes``) read every query and every view,
  procedure and trigger definition through the SQL facade's tokens, in the source
  engine's dialect (:func:`src.tools.sql.facade.sql_tokens`), so a literal, a quoted
  identifier or a comment never matches; ``quoted_with`` reads which quote characters
  open the text's quoted spans, by the same dialect scan;
- schema rules read column data types, defaults and auto-increment flags, table and
  column names, table storage engines and collations, index types and predicates,
  unique keys with a text column and key lengths in bytes (#2227),
  object schemas and procedure languages; ``routines`` lists every procedure,
  function and trigger;
  ``always`` applies to every source of the rule's engines;
- an ``extensions`` rule compares the source's installed extensions
  (``metadata.extensions``, PostgreSQL's ``pg_extension``) with the line the move lands
  on, the lowest one that offers them all, in
  ``src/catalog/aurora_postgresql_extensions.toml`` (#1078, see
  :class:`~src.shared.catalog.gaps.GapMatch`); an extension a rule of its own names
  (TimescaleDB, Citus) is that rule's evidence instead.

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
from typing import Any, Literal

from src.agents.referee.table_resolution import TableNameResolver
from src.shared.catalog.gaps import (
    AURORA_TARGETS,
    GAP_IMPACTS,
    SOURCE_ENGINES,
    AuroraExtensionLine,
    AuroraExtensionLines,
    AuroraVersionLines,
    GapMatch,
    GapRule,
    aurora_extension_lines,
    aurora_version_lines,
    extension_version,
    gap_rules,
    parse_version_line,
)
from src.shared.placeholder_tables import is_placeholder_table
from src.shared.sql_spans import span_dialect, span_settings, with_span_settings
from src.tools.sql.facade import SqlTokens, sql_tokens
from src.tools.sql.normalize import quoted_mask

__all__ = [
    "TARGET_LINE_CHOICES",
    "TargetLineChoice",
    "detect_relational_gaps",
    "extension_landing_line",
    "normalized_data_type",
]


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
    # The characters that open the text's quoted spans (strings, quoted identifiers,
    # comments), as the source dialect reads them.
    quotes: frozenset[str] = frozenset()


@dataclass
class _Evidence:
    tables: set[str] = field(default_factory=set)
    queries: set[str] = field(default_factory=set)
    objects: set[tuple[str, str]] = field(default_factory=set)
    # An object's note (#1078: the line an extension is offered from).
    notes: dict[tuple[str, str], str] = field(default_factory=dict)

    def add_text(self, text: _Text) -> None:
        if text.kind == "query":
            self.queries.add(text.id)
        else:
            self.objects.add((text.kind, text.id))
        self.tables.update(text.tables)

    def __bool__(self) -> bool:
        return bool(self.tables or self.queries or self.objects)


# A ``{value}`` slot in a ``syntax`` entry (see GapMatch.syntax).
_VALUE = "{value}"


def _phrase(text: str) -> tuple[str, ...]:
    """A rule's ``syntax`` entry as tokens, split the way the facade splits SQL; a
    ``{value}`` part stays one slot."""
    out: list[str] = []
    for part in text.split():
        out.extend((_VALUE,) if part == _VALUE else sql_tokens(part, "").tokens)
    return tuple(out)


def _is_value(word: str) -> bool:
    """A ``?`` placeholder, a number or an @variable: what a ``{value}`` slot takes."""
    return word == "?" or word.isdigit() or (word.startswith("@") and word[1:2].isalpha())


def _matches_at(tokens: tuple[str, ...], i: int, phrase: tuple[str, ...]) -> bool:
    if i + len(phrase) > len(tokens):
        return False
    return all(
        _is_value(tokens[i + k]) if want == _VALUE else tokens[i + k] == want
        for k, want in enumerate(phrase)
    )


def _has_phrase(tokens: tuple[str, ...], phrase: tuple[str, ...]) -> bool:
    return any(_matches_at(tokens, i, phrase) for i in range(len(tokens) - len(phrase) + 1))


def _ends_a_statement(tokens: tuple[str, ...], phrase: tuple[str, ...]) -> bool:
    n = len(phrase)
    return any(
        _matches_at(tokens, i, phrase) and (i + n == len(tokens) or tokens[i + n] == ";")
        for i in range(len(tokens) - n + 1)
    )


def _starts_a_statement(tokens: tuple[str, ...], phrase: tuple[str, ...]) -> bool:
    return any(
        (i == 0 or tokens[i - 1] == ";") and _matches_at(tokens, i, phrase)
        for i in range(len(tokens) - len(phrase) + 1)
    )


def _has_prefixed_word(tokens: tuple[str, ...], prefix: str) -> bool:
    """A token that is ``prefix`` and then a letter or "_" (``@total`` for "@", never
    ``@@autocommit``)."""
    n = len(prefix)
    return any(
        tok.startswith(prefix) and len(tok) > n and (tok[n].isalpha() or tok[n] == "_")
        for tok in tokens
    )


def _quote_openers(text: str, engine: str) -> frozenset[str]:
    """The first character of every quoted span of ``text`` in ``engine``'s dialect."""
    mask = quoted_mask(text, engine)
    return frozenset(
        text[i] for i, hidden in enumerate(mask) if hidden and (i == 0 or not mask[i - 1])
    )


def _word_hit(
    tokens: tuple[str, ...],
    word: str,
    unless: frozenset[str],
    unless_after: frozenset[str] = frozenset(),
) -> bool:
    """``word`` appears, and (when ``unless`` names tokens) not directly before one, and
    (when ``unless_after`` does) not directly after one."""
    if not unless and not unless_after:
        return word in tokens
    return any(
        tok == word
        and (i + 1 == len(tokens) or tokens[i + 1] not in unless)
        and (i == 0 or tokens[i - 1] not in unless_after)
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
    after = frozenset(u.upper() for u in match.unless_preceded_by)
    if any(
        w.upper() in words and _word_hit(text.tokens.tokens, w.upper(), unless, after)
        for w in match.words
    ):
        return True
    if _extension_hit(rule, text, user_routines):
        return True
    tokens = text.tokens.tokens
    if any(_ends_a_statement(tokens, _phrase(s)) for s in match.statement_endings):
        return True
    if any(_starts_a_statement(tokens, _phrase(s)) for s in match.statement_starts):
        return True
    if any(_has_prefixed_word(tokens, p) for p in match.word_prefixes):
        return True
    if any(q in text.quotes for q in match.quoted_with):
        return True
    return any(_has_phrase(tokens, _phrase(s)) for s in match.syntax)


def _reads_text(rule: GapRule) -> bool:
    m = rule.match
    return bool(
        m.calls
        or m.words
        or m.syntax
        or m.statement_endings
        or m.statement_starts
        or m.word_prefixes
        or m.quoted_with
        or m.extension_calls
    )


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
    collected schema there is nothing to resolve against: every name stays except a
    placeholder table (``placeholder_tables.is_placeholder_table``, #1817).
    """
    out: set[str] = set()
    for name in names:
        if resolver is None:
            if not is_placeholder_table(name):
                out.add(str(name))
        elif (table_id := resolver.resolve(str(name))) is not None:
            out.add(table_id)
    return tuple(sorted(out))


def _text(
    kind: str,
    text_id: str,
    sql: str,
    engine: str,
    quotes: bool,
    tables: tuple[str, ...] = (),
    sql_mode: object = None,
) -> _Text:
    """One text, tokenized. A MySQL routine or trigger body is read with the
    ``sql_mode`` it was created with (``SQL_MODE`` in information_schema, #1482), which
    the server parses it under; any other text with the collection's settings."""
    own = (
        span_settings(engine, {"sql_mode": sql_mode})
        if isinstance(sql_mode, str) and span_dialect(engine) == "mysql"
        else None
    )
    with with_span_settings(own):
        openers = _quote_openers(sql, engine) if quotes else frozenset()
        return _Text(kind, text_id, sql_tokens(sql, engine), tables, openers)


def _texts(collector: Mapping[str, Any], engine: str, quotes: bool = False) -> list[_Text]:
    """Every query and definition of the collection, tokenized once (and, with
    ``quotes``, its quote openers read)."""
    schema = collector.get("database_schema") or {}
    resolver = TableNameResolver.from_collector(collector)
    out: list[_Text] = []
    for q in _as_list((collector.get("queries") or {}).get("query_patterns")):
        if isinstance(q, Mapping) and q.get("query_id") and q.get("query_text"):
            tables = _collected_tables(_as_list(q.get("tables_accessed")), resolver)
            out.append(
                _text("query", str(q["query_id"]), str(q["query_text"]), engine, quotes, tables)
            )
    for v in _as_list(schema.get("views")):
        if isinstance(v, Mapping) and v.get("view_id") and v.get("definition"):
            out.append(_text("view", str(v["view_id"]), str(v["definition"]), engine, quotes))
    for p in _as_list(schema.get("procedures")):
        if isinstance(p, Mapping) and p.get("procedure_id") and p.get("definition"):
            out.append(
                _text(
                    _routine_kind(p),
                    str(p["procedure_id"]),
                    str(p["definition"]),
                    engine,
                    quotes,
                    sql_mode=p.get("sql_mode"),
                )
            )
    for t in _as_list(schema.get("triggers")):
        if isinstance(t, Mapping) and t.get("trigger_id") and t.get("definition"):
            tables = _collected_tables([t["table_id"]] if t.get("table_id") else [], resolver)
            out.append(
                _text(
                    "trigger",
                    str(t["trigger_id"]),
                    str(t["definition"]),
                    engine,
                    quotes,
                    tables,
                    sql_mode=t.get("sql_mode"),
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
    type_globs = [g.lower() for g in match.data_type_globs]
    defaults = [g.lower() for g in match.default_values]
    engines = {e.lower() for e in match.storage_engines}
    index_types = {t.lower() for t in match.index_types}
    languages = {lang.lower() for lang in match.routine_languages}
    reads_columns = bool(
        types or type_globs or defaults or match.auto_increment or match.uppercase_names
    )
    for table in _as_list(schema.get("tables")):
        if not isinstance(table, Mapping) or not table.get("table_id"):
            continue
        table_id = str(table["table_id"])
        if match.uppercase_names and _has_upper(table.get("table_name")):
            evidence.tables.add(table_id)
        if reads_columns:
            for column in _as_list(table.get("columns")):
                if isinstance(column, Mapping) and _column_hit(
                    match, column, types, type_globs, defaults
                ):
                    evidence.tables.add(table_id)
                    evidence.objects.add(("column", f"{table_id}.{column.get('column_name')}"))
        if index_types or match.partial_indexes:
            for index in _as_list(table.get("indexes")):
                if not isinstance(index, Mapping):
                    continue
                kind = str(index.get("index_type") or "").lower()
                if (kind and kind in index_types) or (
                    match.partial_indexes and str(index.get("predicate") or "").strip()
                ):
                    evidence.tables.add(table_id)
        if match.text_unique_keys or match.index_key_bytes_over is not None:
            for column_name in _key_evidence(match, table):
                evidence.tables.add(table_id)
                evidence.objects.add(("column", f"{table_id}.{column_name}"))
        if _outside_schemas(match, table):
            evidence.tables.add(table_id)
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
        if match.routines or (language and language in languages) or _outside_schemas(match, p):
            evidence.objects.add((_routine_kind(p), str(p["procedure_id"])))
    if match.schemas_other_than:
        for v in _as_list(schema.get("views")):
            if isinstance(v, Mapping) and v.get("view_id") and _outside_schemas(match, v):
                evidence.objects.add(("view", str(v["view_id"])))
        for t in _as_list(schema.get("triggers")):
            if isinstance(t, Mapping) and t.get("trigger_id") and _outside_schemas(match, t):
                evidence.objects.add(("trigger", str(t["trigger_id"])))
    if match.routines:
        for t in _as_list(schema.get("triggers")):
            if isinstance(t, Mapping) and t.get("trigger_id"):
                evidence.objects.add(("trigger", str(t["trigger_id"])))
                evidence.tables.update(
                    _collected_tables([t["table_id"]] if t.get("table_id") else [], resolver)
                )


# #2227: the normalized types that hold text (Column.normalized_data_type), and the ones
# whose key part has a length in characters.
_TEXT_TYPES = frozenset({"string", "text"})
_SIZED_TEXT_TYPE = "string"
# Index kinds the key length matcher skips: a FULLTEXT or SPATIAL key has no key length
# limit (it takes no prefix), and the Aurora MySQL draft leaves GIN, GiST and SP-GiST
# indexes out with a note (``ddl_generator._DROPPED_METHODS``), so no CREATE INDEX runs; a
# SQL Server columnstore index has no key at all, and both drafts leave it out (#2227).
_UNLIMITED_KEY_INDEXES = frozenset({"fulltext", "spatial", "gin", "gist", "spgist", "columnstore"})


def _keys(table: Mapping[str, Any]) -> list[tuple[Mapping[str, Any], bool]]:
    """Each index of ``table`` with whether it enforces uniqueness, then its primary key
    (Table.primary_key; an index that is the primary key too only repeats its evidence)."""
    keys: list[tuple[Mapping[str, Any], bool]] = [
        (index, index.get("is_primary") is True or index.get("is_unique") is True)
        for index in _as_list(table.get("indexes"))
        if isinstance(index, Mapping)
    ]
    if primary_key := [c for c in _as_list(table.get("primary_key")) if c]:
        keys.append(({"columns": primary_key}, True))
    return keys


def _key_evidence(match: GapMatch, table: Mapping[str, Any]) -> list[str]:
    """The key columns of ``table`` that show what the key matchers look for (#2227): the
    text columns of a unique or primary key (``text_unique_keys``), and the text columns
    of a key over the byte limit (``index_key_bytes_over``)."""
    columns = {
        str(c.get("column_name") or "").lower(): c
        for c in _as_list(table.get("columns"))
        if isinstance(c, Mapping)
    }
    found: list[str] = []
    for index, unique in _keys(table):
        names = [str(n) for n in _as_list(index.get("columns")) if n]
        text = [n for n in names if _normalized(columns.get(n.lower())) in _TEXT_TYPES]
        if match.text_unique_keys and unique:
            found += text
        limit, per_char = match.index_key_bytes_over, match.index_key_bytes_per_character
        if limit is None or per_char is None:
            continue
        if str(index.get("index_type") or "").lower() in _UNLIMITED_KEY_INDEXES:
            continue
        prefixes = _as_list(index.get("prefix_lengths"))
        chars = 0
        for pos, name in enumerate(names):
            column = columns.get(name.lower())
            if column is None or _normalized(column) != _SIZED_TEXT_TYPE:
                continue
            prefix = _chars(prefixes[pos] if pos < len(prefixes) else None)
            chars += prefix or _chars(column.get("max_length"))
        if chars * per_char > limit:
            found += text
    return found


def _normalized(column: Mapping[str, Any] | None) -> str:
    return str((column or {}).get("normalized_data_type") or "").lower()


def _chars(value: Any) -> int:
    """A collected length in characters, or 0 when there is none."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _has_upper(name: Any) -> bool:
    return any(ch.isupper() for ch in str(name or ""))


def _column_hit(
    match: GapMatch,
    column: Mapping[str, Any],
    types: set[str],
    type_globs: list[str],
    defaults: list[str],
) -> bool:
    """Whether one collected column shows what the rule's column matchers look for."""
    data_type = normalized_data_type(column.get("data_type"))
    if data_type in types or any(fnmatch.fnmatchcase(data_type, g) for g in type_globs):
        return True
    default = column.get("default_value")
    if defaults and default is not None and not isinstance(default, bool):
        text = str(default).strip().lower()
        if any(fnmatch.fnmatchcase(text, g) for g in defaults):
            return True
    if match.auto_increment and column.get("is_auto_increment") is True:
        return True
    return match.uppercase_names and _has_upper(column.get("column_name"))


def _outside_schemas(match: GapMatch, obj: Mapping[str, Any]) -> bool:
    """A collected object whose schema is set and not one of ``schemas_other_than``."""
    if not match.schemas_other_than:
        return False
    schema = str(obj.get("schema_name") or "").strip().lower()
    return bool(schema) and schema not in {s.lower() for s in match.schemas_other_than}


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


def _installed_extensions(collector: Mapping[str, Any]) -> dict[str, str | None] | None:
    """The source's installed extensions, lower-case name -> version, or None when the
    collection has no extension list (collected before #1078, or not PostgreSQL)."""
    raw = (collector.get("metadata") or {}).get("extensions")
    if not isinstance(raw, list):
        return None
    out: dict[str, str | None] = {}
    for entry in raw:
        if isinstance(entry, Mapping) and str(entry.get("name") or "").strip():
            version = entry.get("version")
            out[str(entry["name"]).strip().lower()] = (
                str(version) if version not in (None, "") else None
            )
    return out


_Line = tuple[tuple[int, ...], str, AuroraExtensionLine]

# The Aurora version line a move targets (#1951, ADR-034 Decision 3): the line the
# assessment chooses (``same_major``, the default: the source's own major unless an
# extension needs a later line), or the newest listed line, the customer's option.
TargetLineChoice = Literal["same_major", "newest"]
TARGET_LINE_CHOICES: tuple[TargetLineChoice, ...] = ("same_major", "newest")


@dataclass(frozen=True)
class _Landing:
    """Where a move with these extensions lands: the lines it can move to
    (``candidates``), the source's own listed line (``own``) and the lowest candidate
    that offers every extension (``chosen``, None when none does)."""

    candidates: list[_Line]
    own: _Line | None
    chosen: _Line | None


def _fits(
    server: frozenset[str], entry: AuroraExtensionLine, name: str, version: str | None
) -> bool:
    if name not in entry.extensions:
        return False
    if name in server:
        return True
    have, top = extension_version(version), extension_version(entry.extensions[name])
    return have is None or top is None or have <= top


def _landing(
    considered: Mapping[str, str | None],
    offered: AuroraExtensionLines,
    line: tuple[int, ...] | None,
) -> _Landing | None:
    """The line choice of :func:`_extension_findings` (see there), or None when the list
    names no line."""
    parsed = sorted(
        (key, name, entry)
        for name, entry in offered.lines.items()
        if (key := parse_version_line("postgresql", name)) is not None
    )
    if not parsed:
        return None
    server = frozenset(offered.server_modules)
    own = None
    if line is None or line < parsed[0][0]:
        candidates = parsed
    elif line > parsed[-1][0]:
        candidates = [parsed[-1]]
    else:
        candidates = [p for p in parsed if p[0] >= line]
        own = next((p for p in parsed if p[0] == line), None)
    chosen = next(
        (c for c in candidates if all(_fits(server, c[2], n, v) for n, v in considered.items())),
        None,
    )
    return _Landing(candidates, own, chosen)


def _own_rule_extensions(
    engine: str, target_engine: str, rules: Iterable[GapRule]
) -> frozenset[str]:
    """The extensions a gap rule of this move names (that rule's evidence, not the
    generic extension rules')."""
    return frozenset(
        r.match.extension.lower()
        for r in rules
        if engine in r.source_engines and target_engine in r.targets and r.match.extension
    )


def extension_landing_line(
    installed: Mapping[str, str | None] | None,
    source_engine: str,
    source_version: str | None,
    target_engine: str,
    *,
    rules: Iterable[GapRule] | None = None,
    extension_lines: Mapping[str, AuroraExtensionLines] | None = None,
    target_choice: TargetLineChoice = "same_major",
) -> str | None:
    """The Aurora version line a ``source_engine`` source (the collector's engine, as
    :func:`detect_relational_gaps` reads it) with these installed extensions
    (pg_extension name -> version) moves to, as the gap check chooses it
    (#1078): the lowest line, among the ones the source can move to, that offers every
    installed extension at its version, extensions a rule of their own names (TimescaleDB,
    Citus) aside. When no line offers them all: the source's own line when it is listed,
    else the newest line it can move to. None when ``target_engine`` has no extension
    list for ``source_engine``. ``rules`` and ``extension_lines`` default to the catalog's.

    ``target_choice`` (#1951): ``same_major``, the default, is the choice above;
    ``newest`` is the customer's option to move to the newest listed line instead, whatever
    the source's line and extensions (the extension checks then run against that line).
    """
    if target_choice not in TARGET_LINE_CHOICES:
        raise ValueError(f"target_choice {target_choice!r} is not one of {TARGET_LINE_CHOICES}")
    offered = (extension_lines if extension_lines is not None else aurora_extension_lines()).get(
        target_engine
    )
    if offered is None or source_engine not in offered.source_engines:
        return None
    own_rules = _own_rule_extensions(
        source_engine, target_engine, gap_rules() if rules is None else rules
    )
    considered = {str(n).strip().lower(): v for n, v in (installed or {}).items() if str(n).strip()}
    considered = {n: v for n, v in considered.items() if n not in own_rules}
    landing = _landing(considered, offered, parse_version_line(source_engine, source_version))
    if landing is None:
        return None
    if target_choice == "newest":
        return landing.candidates[-1][1]
    line = landing.chosen or landing.own or landing.candidates[-1]
    return line[1]


def _extension_findings(
    installed: Mapping[str, str | None] | None,
    offered: AuroraExtensionLines | None,
    line: tuple[int, ...] | None,
    own_rules: frozenset[str],
) -> dict[str, dict[str, str | None]]:
    """What each ``extensions`` check finds: check -> {extension: note}.

    The move lands on the lowest line, among the ones the source can move to, that
    offers every installed extension at its version (a server module's name only): for a
    source on a listed line its own line or a later one, for one older than every line or
    of unknown version any line, for one newer than every line the newest. When that line
    is above a listed source's own, the extensions its own line lacks are noted with the
    line that offers them (``on_a_later_line``). When no line offers them all, each one is
    checked against the source's own line (any line, or the newest, for the other
    sources). An extension a rule of its own names (``own_rules``) is left to that rule.
    """
    out: dict[str, dict[str, str | None]] = {
        "not_offered": {},
        "newer_than_offered": {},
        "on_a_later_line": {},
    }
    considered = {n: v for n, v in (installed or {}).items() if n not in own_rules}
    if offered is None or not considered:
        return out
    landing = _landing(considered, offered, line)
    if landing is None:
        return out
    server = frozenset(offered.server_modules)
    own, chosen = landing.own, landing.chosen
    if chosen is not None:
        if own is not None and chosen[0] > own[0]:
            for name, version in considered.items():
                if not _fits(server, own[2], name, version):
                    out["on_a_later_line"][name] = f"offered from Aurora PostgreSQL {chosen[1]}"
        return out
    reference = [own[2]] if own is not None else [c[2] for c in landing.candidates]
    for name, version in considered.items():
        listed = [e.extensions[name] for e in reference if name in e.extensions]
        if not listed:
            out["not_offered"][name] = None
        elif name not in server:
            have = extension_version(version)
            top = max((v for x in listed if (v := extension_version(x)) is not None), default=None)
            if have is not None and top is not None and have > top:
                out["newer_than_offered"][name] = None
    return out


def detect_relational_gaps(
    collector: Mapping[str, Any] | None,
    target_engine: str | None,
    *,
    rules: Iterable[GapRule] | None = None,
    versions: Mapping[str, AuroraVersionLines] | None = None,
    extension_lines: Mapping[str, AuroraExtensionLines] | None = None,
) -> dict[str, Any] | None:
    """The relational move's version and feature gaps (``RelationalGapFacts``), or None
    when there is no collection, no known source engine or no Aurora target.

    ``target_engine`` is wave 1's Aurora engine (``retained_engine_for``). ``rules``,
    ``versions`` and ``extension_lines`` default to the catalog's.
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
    needs_quotes = any(r.match.quoted_with for r in applicable)
    texts = _texts(collector, engine, needs_quotes) if needs_text else []
    word_sets = [frozenset(t.tokens.tokens) for t in texts]

    resolver = TableNameResolver.from_collector(collector)
    installed = _installed_extensions(collector)
    # #1078: the extensions the target offers on the lines this source can move to, and
    # the ones a rule of this move names (that rule's evidence, not the generic one's).
    offered = (extension_lines if extension_lines is not None else aurora_extension_lines()).get(
        str(target_engine)
    )
    own_rules = _own_rule_extensions(engine, str(target_engine), applicable)
    findings = _extension_findings(
        installed,
        offered if offered is not None and engine in offered.source_engines else None,
        parse_version_line(engine, version),
        own_rules,
    )
    gaps: list[dict[str, Any]] = []
    for rule in applicable:
        evidence = _Evidence()
        match = rule.match
        if match.version is not None:
            if not (version_checked and _version_hit(match.version, line, parsed_lines)):
                continue
        elif match.extensions is not None:
            # A collection with no extension list (None) finds nothing.
            for name, note in findings[match.extensions].items():
                evidence.objects.add(("extension", name))
                if note:
                    evidence.notes[("extension", name)] = note
            if not evidence:
                continue
        elif not match.always:
            if _reads_text(rule):
                user_routines = _user_routines(rule, collector)
                for text, words in zip(texts, word_sets, strict=True):
                    if _text_hit(rule, text, words, user_routines):
                        evidence.add_text(text)
            _schema_evidence(rule, collector, evidence, resolver)
            if match.extension and installed and match.extension.lower() in installed:
                # #1078: the extension itself is installed.
                evidence.objects.add(("extension", match.extension.lower()))
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
                # Every field GapObject dumps, ``note`` None when there is none: report.json
                # stores these facts through the contract, and the facts layer recomputes
                # them from this function (ADR-032), so the two must be the same dict.
                "objects": [
                    {"kind": k, "id": i, "note": evidence.notes.get((k, i))}
                    for k, i in sorted(evidence.objects)
                ],
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
        # conversion the catalog has none for is not checked for feature gaps, which
        # the wave note says (every conversion has some since #1330).
        "feature_rules_checked": sum(1 for r in applicable if r.match.version is None),
        "gaps": gaps,
        "gaps_by_impact": {
            impact: sum(1 for g in gaps if g["impact"] == impact) for impact in GAP_IMPACTS
        },
    }
