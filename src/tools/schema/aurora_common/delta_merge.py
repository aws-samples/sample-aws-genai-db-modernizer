# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Merge a model-written delta into the deterministic Aurora draft (issue #273).

The model never transcribes the draft. It writes an ``AuroraDesignDeltaContract``
(index changes, column-type overrides, residual rules by source type,
optimizations, app-layer notes, trade-offs); this module rebuilds the draft
from the same projected input the draft builder uses, applies the delta,
regenerates the DDL and returns a full ``Aurora*ModelOutputContract`` dict.

Pure and deterministic: the same input and delta always give the same output.
Every reference the delta makes (table, column, index) is checked against the
draft; an unknown one is a merge error, never silently dropped (unless the
caller asks for a lenient merge, which records the errors on the output).

Nothing from the model reaches DDL unparsed: types pass the strict per-engine
type grammar, and indexes are rendered from structured parts with quoted
identifiers (``ddl_generator.render_index``); see ``sql_safety``. A legacy
full-contract response goes through ``full_contract_to_delta`` first, so its
DDL text is ignored and regenerated from the draft as well.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from pydantic import BaseModel, ValidationError

from src.contracts.aurora_design_delta import (
    INDEX_METHODS,
    AuroraDesignDeltaContract,
    IndexSpec,
    TypeRule,
    validate_aurora_type,
)
from src.contracts.aurora_mysql_model_output import AuroraMySQLModelOutputContract
from src.contracts.aurora_postgresql_model_output import AuroraPostgresqlModelOutputContract
from src.contracts.schema_design_input import (
    AgentAnalysisInput,
    AgentCollectorInput,
    AgentIndex,
    AgentQueryPattern,
    AgentTable,
)
from src.shared.left_out_objects import with_not_created_comments
from src.shared.table_keys import DesignTableKeys, spans_schemas, table_key
from src.tools.schema.aurora_common.access_patterns import derive_access_patterns
from src.tools.schema.aurora_common.ddl_generator import (
    MYSQL,
    POSTGRES,
    ColumnDDL,
    DdlResult,
    Dialect,
    TableDDL,
    TypeOverride,
    TypeOverrides,
    _key_prefix_note,
    _not_created_reason,
    assemble_full_ddl,
    carried_indexes,
    comment_text,
    design_table_name,
    generate_mysql_ddl,
    generate_pg_ddl,
    mysql_key_plan,
    pg_method_conflict,
    quotable_tables,
    render_index,
    rendered_method,
    secondary_indexes,
)
from src.tools.schema.aurora_common.source_family import migration_strategy
from src.tools.schema.aurora_common.sql_safety import (
    SqlFragmentError,
    parse_index_statement,
    render_predicate,
    resolve_column,
)

_GENERATORS = {"aurora_postgresql": generate_pg_ddl, "aurora_mysql": generate_mysql_ddl}
_DIALECTS = {"aurora_postgresql": POSTGRES, "aurora_mysql": MYSQL}


# The contract each engine's merged output is validated against; its
# ``contract_version`` default is the version the merge writes (#974).
_OUTPUT_CONTRACTS: dict[str, type[BaseModel]] = {
    "aurora_mysql": AuroraMySQLModelOutputContract,
    "aurora_postgresql": AuroraPostgresqlModelOutputContract,
}


def _schema_of(name: str) -> str | None:
    cleaned = "".join(c for c in name if c not in '`"[]').strip().lower()
    return cleaned.rsplit(".", 1)[0].strip() if "." in cleaned else None


@dataclass
class AuroraDesignBase:
    """Everything the draft is built from, plus the source types residual rules match."""

    engine: str
    job_id: str
    source_database: str
    source_engine: str
    tables: list[AgentTable]
    source_data_types: dict[tuple[str, str], str] = field(default_factory=dict)
    queries: list[AgentQueryPattern] = field(default_factory=list)
    """The observed query log, projected onto access patterns by the merge.

    Defaulted rather than required so every existing construction site keeps
    working: an empty log yields no access patterns, which is the same output
    those callers produce today.
    """
    left_out: list[dict] = field(default_factory=list)
    """The source objects taken out of ``tables`` because their name cannot be quoted into
    SQL (#2381 review 2) or another index of the table has it (#2402), by
    ``ddl_generator.quotable_tables``, kept across
    ``dataclasses.replace``. Never written into DDL; each becomes an app-layer note."""
    schema_qualified: bool | None = None
    """Whether the draft creates each table in its source schema (#2455): None decides it
    from ``tables`` (more than one schema among them); step 1 passes False. With it, the
    draft names each table ``<schema>.<table>`` (``name_of``), and ``source_data_types``
    and the type overrides are keyed on that name."""

    def __post_init__(self) -> None:
        if self.schema_qualified is None:
            self.schema_qualified = spans_schemas(t.schema_name for t in self.tables)
        self.tables, left_out = quotable_tables(self.tables, self.dialect, self.qualified)
        self.left_out = [*self.left_out, *left_out]

    @property
    def qualified(self) -> bool:
        return bool(self.schema_qualified)

    def name_of(self, table: AgentTable) -> str:
        """The draft's name for ``table`` (``ddl_generator.design_table_name``)."""
        return design_table_name(table, self.qualified)

    def schema_of(self, table: AgentTable) -> str | None:
        """The schema the draft creates ``table`` in, None in a design of one schema."""
        return table.schema_name if self.qualified else None

    def key_of(self, table: AgentTable) -> str:
        """``table``'s key (``table_keys.table_key``)."""
        return table_key(table.table_name, self.schema_of(table))

    def table_keys(self) -> DesignTableKeys:
        return DesignTableKeys(
            ((t.table_name, self.schema_of(t)) for t in self.tables), self.source_database
        )

    @property
    def migration_strategy(self) -> str:
        return migration_strategy(self.source_engine, self.engine)  # type: ignore[arg-type]

    @property
    def dialect(self) -> Dialect:
        return _DIALECTS[self.engine]

    @classmethod
    def from_inputs(
        cls,
        engine: str,
        agent_collector: AgentCollectorInput,
        raw_collector: dict | None = None,
    ) -> AuroraDesignBase:
        """``agent_collector`` is the projected input; ``raw_collector`` supplies ``data_type``."""
        qualified = spans_schemas(t.schema_name for t in agent_collector.tables)
        return cls(
            engine=engine,
            job_id=agent_collector.job_id,
            source_database=agent_collector.source_database_name,
            source_engine=agent_collector.source_database_engine,
            tables=list(agent_collector.tables),
            source_data_types=source_data_types(raw_collector or {}, qualified=qualified),
            queries=list(agent_collector.queries.query_patterns),
            schema_qualified=qualified,
        )

    def generate(self, type_overrides: TypeOverrides | None = None) -> DdlResult:
        return _GENERATORS[self.engine](
            self.tables, type_overrides, self.source_engine, self.qualified
        )

    def fingerprint(self) -> str:
        """Hash of everything the draft is built from.

        The external request records it and ``--finalize`` compares it with the
        rebuilt base, so a delta is never merged into a different draft than
        the one its view described.
        """
        payload = {
            "engine": self.engine,
            "source_engine": self.source_engine,
            "tables": [t.model_dump(mode="json") for t in self.tables],
            "source_data_types": sorted(
                f"{t}.{c}={v}" for (t, c), v in self.source_data_types.items()
            ),
            # Only when set, so a one-schema draft keeps the fingerprint it always had.
            **({"schema_qualified": True} if self.qualified else {}),
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return f"sha256:{digest}"


@dataclass
class _Tables:
    """The draft's tables under the draft's names (``AuroraDesignBase.name_of``), each found
    by any spelling a delta uses: ``discourse.users``, ``"users"`` and ``USERS`` all find
    draft table ``users``; in a design of several schemas (#2455) ``sales.orders`` finds
    that schema's ``orders``, and a bare ``orders`` two schemas have finds none."""

    named: list[AgentTable]
    refs: dict[str, tuple[str, str | None]]  # draft name -> (table, schema) the SQL names
    by_key: dict[str, AgentTable]
    keys: DesignTableKeys

    @classmethod
    def of(cls, base: AuroraDesignBase) -> _Tables:
        named: list[AgentTable] = []
        refs: dict[str, tuple[str, str | None]] = {}
        by_key: dict[str, AgentTable] = {}
        for t in base.tables:
            copy = t.model_copy(update={"table_name": base.name_of(t)})
            named.append(copy)
            refs[copy.table_name] = (t.table_name, base.schema_of(t))
            by_key[base.key_of(t)] = copy
        return cls(named, refs, by_key, base.table_keys())

    def find(self, name: str) -> AgentTable | None:
        key = self.keys.key_of(name)
        return self.by_key.get(key) if key is not None else None


def _unknown_table(name: str, tables: _Tables, hint: str = "") -> str:
    """Why a delta's table name finds no draft table: none has it, or two schemas do."""
    bare = name.replace('"', "").replace("`", "").rsplit(".", 1)[-1].strip().lower()
    schemas = sorted(
        str(schema) for table, schema in tables.refs.values() if schema and table.lower() == bare
    )
    if len(schemas) > 1:
        return (
            f"table '{name}' is in more than one schema ({', '.join(schemas)}); name it as "
            "<schema>.<table>, as the design view does"
        )
    return f"unknown table '{name}'; the draft has no such table{hint}"


def draft_index_names(table: AgentTable, dialect: Dialect) -> list[str]:
    """Names of the draft's secondary indexes on ``dialect``, in ``index_sql`` order."""
    return [i.index_name for i in carried_indexes(table, dialect)]


def source_data_types(
    raw_collector: dict, *, qualified: bool = False
) -> dict[tuple[str, str], str]:
    """``(table_name, column_name) -> data_type`` from a raw collector output dict; with
    ``qualified`` (#2455) a table with a schema is ``<schema>.<table>``, as the draft
    names it."""
    types: dict[tuple[str, str], str] = {}
    for table in (raw_collector.get("database_schema") or {}).get("tables") or []:
        name = str(table.get("table_name", ""))
        if qualified and table.get("schema_name"):
            name = f"{table['schema_name']}.{name}"
        for col in table.get("columns") or []:
            if col.get("data_type"):
                types[(name, col.get("column_name", ""))] = str(col["data_type"])
    return types


@dataclass
class MergeResult:
    output: dict | None
    errors: list[str]
    summary: dict
    warnings: list[str] = field(default_factory=list)


def _check_dialect_type(engine: str, aurora_type: str, where: str, errors: list[str]) -> bool:
    """The contract checked the type for either engine; check it for this one."""
    try:
        validate_aurora_type(aurora_type, engine)
    except ValueError as exc:
        errors.append(f"{where}: {exc}")
        return False
    return True


def _type_overrides(
    base: AuroraDesignBase,
    delta: AuroraDesignDeltaContract,
    tables: _Tables,
    residuals: list[dict],
    errors: list[str],
    warnings: list[str],
) -> tuple[TypeOverrides, int]:
    overrides: TypeOverrides = {}
    for i, change in enumerate(delta.tables):
        table = tables.find(change.table_name)
        if table is None:
            continue  # reported by the caller
        columns = [c.column_name for c in table.columns]
        for j, col_change in enumerate(change.column_types):
            where = f"tables[{i}].column_types[{j}]"
            column = resolve_column(columns, col_change.column)
            if column is None:
                errors.append(
                    f"{where}: table '{table.table_name}' has no column '{col_change.column}'"
                )
                continue
            if not _check_dialect_type(base.engine, col_change.aurora_type, where, errors):
                continue
            overrides[(table.table_name, column)] = TypeOverride(
                col_change.aurora_type, col_change.reason or "Set by the schema designer."
            )

    rules: dict[str, TypeRule] = {}
    for k, type_rule in enumerate(delta.type_rules):
        folded = type_rule.source_data_type.strip().lower()
        if folded in rules:
            errors.append(
                f"type_rules[{k}]: source_data_type '{type_rule.source_data_type}' is listed "
                "more than once (rules match case-insensitively)"
            )
            continue
        if _check_dialect_type(base.engine, type_rule.aurora_type, f"type_rules[{k}]", errors):
            rules[folded] = type_rule
    by_rule = 0
    used: set[str] = set()
    for residual in residuals:
        key = (residual["table"], residual["column"])
        if key in overrides:
            continue
        raw = base.source_data_types.get(key, "").strip().lower()
        rule = rules.get(raw)
        if rule is not None:
            overrides[key] = TypeOverride(
                rule.aurora_type,
                rule.reason or f"Residual {raw} columns resolved by the schema designer.",
            )
            used.add(raw)
            by_rule += 1
    for raw, rule in rules.items():
        if raw not in used:
            warnings.append(
                f"type_rules: source_data_type '{rule.source_data_type}' matched no residual "
                "column (see residual_types)"
            )
    return overrides, by_rule


@dataclass
class _Index:
    name: str
    columns: list[str]
    unique: bool
    method: str | None
    include: list[str]
    where_sql: str | None
    prefix_lengths: list[int | None] | None = None
    notes: list[dict] = field(default_factory=list)


def _draft_index(table: AgentTable, name: str) -> AgentIndex | None:
    """The source index the draft carries under ``name`` (any case), if any."""
    hits = [i for i in secondary_indexes(table) if i.index_name.lower() == name.lower()]
    return hits[0] if len(hits) == 1 else None


def _structured_index(
    base: AuroraDesignBase,
    table: AgentTable,
    entry: IndexSpec | str,
    column_ddls: list[ColumnDDL],
    *,
    modify: bool = False,
    tables: _Tables,
) -> _Index:
    """Validate one delta index entry against the table, its merged column types
    (``column_ddls``) and the dialect. Raises SqlFragmentError.

    ``modify``: the entry replaces the draft index of the same name, so on Aurora MySQL it
    keeps that index's FULLTEXT or SPATIAL kind where the new columns take it (#1596): a
    delta cannot spell either kind, and neither is ever UNIQUE."""
    engine = base.engine
    columns = [c.column_name for c in table.columns]
    if isinstance(entry, str):
        parsed = parse_index_statement(entry)
        spelled = f"{parsed.schema}.{parsed.table}" if parsed.schema else parsed.table
        if tables.find(spelled) is not table:
            raise SqlFragmentError(
                f"index '{parsed.index_name}' is not ON table '{table.table_name}'"
            )
        if parsed.schema is not None:
            allowed = {s.lower() for s in (table.schema_name, base.source_database) if s}
            if parsed.schema.lower() not in allowed:
                raise SqlFragmentError(
                    f"schema prefix '{parsed.schema}' does not match table '{table.table_name}'"
                )
        if engine == "aurora_mysql":
            if parsed.pg_only:
                raise SqlFragmentError(
                    f"{', '.join(parsed.pg_only)} is not Aurora MySQL index syntax"
                )
            if '"' in parsed.quotes:
                raise SqlFragmentError('Aurora MySQL identifiers are quoted with backticks, not "')
        elif "`" in parsed.quotes:
            raise SqlFragmentError('Aurora PostgreSQL identifiers are quoted with ", not backticks')
        if parsed.method is not None and parsed.method not in INDEX_METHODS:
            raise SqlFragmentError(f"unknown index method '{parsed.method}'")
        spec = IndexSpec(
            index_name=parsed.index_name,
            columns=parsed.columns,
            unique=parsed.unique,
            method=parsed.method,  # type: ignore[arg-type]
        )
    else:
        spec = entry
    if engine == "aurora_mysql" and (spec.method or spec.include or spec.where):
        raise SqlFragmentError("method, include and where are Aurora PostgreSQL only")
    resolved = []
    for name in [*spec.columns, *spec.include]:
        column = resolve_column(columns, name)
        if column is None:
            raise SqlFragmentError(f"table '{table.table_name}' has no column '{name}'")
        resolved.append(column)
    index_columns = resolved[: len(spec.columns)]
    # An entry replaces the whole definition (the skill says so, and the design view shows
    # each index's INCLUDE to repeat, #2227): ``include`` [] has none. A column the entry
    # also keys is not repeated in INCLUDE.
    include = [c for c in resolved[len(spec.columns) :] if c not in index_columns]
    if spec.method:  # Aurora PostgreSQL only: refused above on Aurora MySQL
        why = pg_method_conflict(
            spec.method,
            spec.unique,
            index_columns,
            column_ddls,
            no_opclass_because="the design delta cannot name one",
        )
        if not why and spec.include and spec.method not in ("btree", "gist", "spgist"):
            why = "only a B-tree, GiST or SP-GiST index takes INCLUDE columns in PostgreSQL"
        if why:
            raise SqlFragmentError(f"index '{spec.index_name}' cannot use {spec.method}: {why}")
    where_sql = (
        render_predicate(spec.where, columns, base.dialect.q) if spec.where is not None else None
    )
    prefixes = None
    unique = spec.unique
    method: str | None = spec.method
    notes: list[dict] = []
    if base.dialect is MYSQL:
        # #1623: Aurora MySQL needs a key prefix on a TEXT or BLOB column, within 3072 bytes.
        # A modified draft index keeps the prefixes its source recorded.
        draft = _draft_index(table, spec.index_name)
        by_column = (
            {c.lower(): n for c, n in zip(draft.columns, draft.prefix_lengths or [], strict=False)}
            if draft
            else {}
        )
        if modify and draft is not None and not spec.unique:
            moved = draft.model_copy(update={"columns": index_columns})
            method = rendered_method(moved, MYSQL, column_ddls)
        # A FULLTEXT or SPATIAL index the merge keeps takes no prefix; one it renders as a
        # B-tree takes one like any other (#1623)
        plan = mysql_key_plan(
            index_columns, [by_column.get(c.lower()) for c in index_columns], column_ddls, method
        )
        if not plan.fits:
            raise SqlFragmentError(
                f"index '{spec.index_name}' cannot be created: {_not_created_reason(plan)}"
            )
        prefixes = plan.prefixes
        if plan.weakens_unique:
            notes.append(_key_prefix_note(table.table_name, spec.index_name, plan, unique))
            unique = False
    return _Index(
        name=spec.index_name,
        columns=index_columns,
        unique=unique,
        notes=notes,
        method=method,
        include=include,
        where_sql=where_sql,
        prefix_lengths=prefixes,
    )


def _render(base: AuroraDesignBase, ref: tuple[str, str | None], index: _Index) -> str:
    """``index`` on the table ``ref`` names: its (table, schema), as ``_Tables.refs``."""
    return render_index(
        base.dialect,
        ref[0],
        index.name,
        index.columns,
        unique=index.unique,
        method=index.method,
        include=index.include,
        where_sql=index.where_sql,
        prefix_lengths=index.prefix_lengths,
        schema=ref[1],
    )


def _lookup(current: dict[str, str], name: str) -> str | None:
    if name in current:
        return name
    hits = [n for n in current if n.lower() == name.lower()]
    return hits[0] if len(hits) == 1 else None


def _apply_index_changes(
    base: AuroraDesignBase,
    delta: AuroraDesignDeltaContract,
    tables: _Tables,
    table_ddls: dict[str, TableDDL],
    draft_indexes: dict[str, list[str]],
    errors: list[str],
    index_notes: list[dict],
) -> dict[str, int]:
    counts = {"added": 0, "modified": 0, "removed": 0}
    for i, change in enumerate(delta.tables):
        table = tables.find(change.table_name)
        if table is None:
            continue
        ref = tables.refs[table.table_name]
        ddl = table_ddls[table.table_name]
        # name -> statement, in draft order (names come from the source, not the model)
        current: dict[str, str] = dict(
            zip(draft_indexes[table.table_name], ddl.index_sql, strict=True)
        )

        for j, name in enumerate(change.remove_indexes):
            existing = _lookup(current, name.strip().strip('"`'))
            if existing is None:
                errors.append(
                    f"tables[{i}].remove_indexes[{j}]: table '{table.table_name}' has no "
                    f"index '{name}'"
                )
                continue
            del current[existing]
            _drop_index_notes(index_notes, table.table_name, existing)
            counts["removed"] += 1

        for j, entry in enumerate(change.modify_indexes):
            where = f"tables[{i}].modify_indexes[{j}]"
            try:
                index = _structured_index(
                    base, table, entry, ddl.columns, modify=True, tables=tables
                )
            except (SqlFragmentError, ValidationError) as exc:
                errors.append(f"{where}: {exc}")
                continue
            existing = _lookup(current, index.name)
            if existing is None:
                errors.append(
                    f"{where}: table '{table.table_name}' has no index '{index.name}' to modify "
                    "(modify_indexes is matched by index_name; use add_indexes for a new one)"
                )
                continue
            current[existing] = _render(base, ref, index)
            _drop_index_notes(index_notes, table.table_name, existing)
            index_notes.extend({**n, "index": existing} for n in index.notes)
            counts["modified"] += 1

        for j, entry in enumerate(change.add_indexes):
            where = f"tables[{i}].add_indexes[{j}]"
            try:
                index = _structured_index(base, table, entry, ddl.columns, tables=tables)
            except (SqlFragmentError, ValidationError) as exc:
                errors.append(f"{where}: {exc}")
                continue
            if _lookup(current, index.name) is not None:
                errors.append(
                    f"{where}: index '{index.name}' already exists on '{table.table_name}'"
                )
                continue
            current[index.name] = _render(base, ref, index)
            index_notes.extend(index.notes)
            counts["added"] += 1

        ddl.index_sql = list(current.values())
        draft_indexes[table.table_name] = list(current)
    return counts


def _drop_index_notes(notes: list[dict], table: str, index: str) -> None:
    """Forget the draft's notes about an index the delta removed or replaced."""
    notes[:] = [
        n for n in notes if not (n["table"] == table and str(n["index"]).lower() == index.lower())
    ]


def _left_out_notes(base: AuroraDesignBase) -> list[dict]:
    """One app-layer note per source object the draft does not create because its name
    cannot be quoted into SQL (#2381 review 2) or another index of its table has it
    (#2402, feature ``duplicate_index_name``); the names cleaned like a comment's. A table
    is named once (#2484: ``Sales.orders``, not ``Sales.orders.Sales.orders``), any other
    object ``table.object``."""
    return [
        {
            "feature": o.get("feature", "unsafe_name"),
            "source_object": comment_text(
                o["table"] if o["kind"] == "table" else f"{o['table']}.{o['name']}"
            ),
            "recommendation": f"{o['kind'].capitalize()} not created: {o['reason']}.",
        }
        for o in base.left_out
    ]


def _index_notes(ddl: DdlResult) -> list[dict]:
    """One deterministic app-layer note per index whose key prefix the draft added, cut or
    that lost its UNIQUE because of one (#1623), so the deliverables say so."""
    return [
        {
            "feature": "index_key_prefix",
            "source_object": f"{n['table']}.{n['index']}",
            "recommendation": n["reason"],
        }
        for n in ddl.index_notes
        if n.get("kind") == "key_prefix"
    ]


def _time_zone_notes(ddl: DdlResult) -> list[dict]:
    """One deterministic app-layer note for time-zone-aware columns mapped to DATETIME/TIME."""
    cols = [f"{n['table']}.{n['column']}" for n in ddl.column_notes if n["kind"] == "time_zone"]
    if not cols:
        return []
    shown = ", ".join(cols[:10]) + (f" and {len(cols) - 10} more" if len(cols) > 10 else "")
    return [
        {
            "feature": "time_zone",
            "source_object": shown,
            "recommendation": (
                "These PostgreSQL timestamptz/timetz columns become DATETIME/TIME on Aurora "
                "MySQL, which store no time zone: convert to UTC during the data load and "
                "have the application write and read UTC (or set time_zone='+00:00' on its "
                "connections)."
            ),
        }
    ]


def _default_notes(ddl: DdlResult) -> list[dict]:
    """One deterministic app-layer note per source DEFAULT the DDL dropped or rewrote (#156).

    The reason already quotes the source text through ``excerpt`` and, for a
    sequence turned into an identity column, says to restart it above the
    highest migrated value, so the note carries that step into the deliverables.
    """
    return [
        {
            "feature": "column_default",
            "source_object": f"{n['table']}.{n['column']}",
            "recommendation": n["reason"],
        }
        for n in ddl.column_notes
        if n["kind"] == "default"
    ]


# #1250: says what is missing and that the design does not depend on it.
_FILTER_UNKNOWN = (
    "filter predicate unknown (not readable from the collected query text); "
    "only the serving index is unconfirmed, the tables and DDL do not depend on it"
)
_NO_LEADING_INDEX = "no index leads with the filtered column(s)"
_AMBIGUOUS_INDEX = "ambiguous index choice"
_NOT_IN_DRAFT = "the draft does not create the source index that serves it"


def _residual_category(reason: str) -> str:
    """A short, stable label for one access-pattern residual's reason.

    Matches ``access_patterns``' four residual shapes by
    their fixed opening clause, not the full sentence (which carries
    per-pattern detail -- table/index names -- that would defeat grouping).
    """
    if reason.startswith("Filter predicate unknown"):
        return _FILTER_UNKNOWN
    if reason.startswith("No index leads with any filtered column"):
        return _NO_LEADING_INDEX
    if reason.startswith("The draft does not create"):
        return _NOT_IN_DRAFT
    return _AMBIGUOUS_INDEX


def _access_pattern_residual_warnings(residuals: list[dict]) -> list[str]:
    """One compact warning for every access-pattern residual, not one each.

    Headless stdout must stay compact (AGENTS.md): on a real workload, most
    residuals are the same handful of reason shapes repeated per query (a
    wordpress run case has 42 "filter predicate unknown" residuals), so one
    line per pattern id turned a single ``--finalize`` status line into
    dozens (#157 review, I4). Each pattern's own ``judgment_reason`` already
    carries the full detail in the merged output's ``access_patterns`` --
    this is a count by category, not a replacement for it.
    """
    if not residuals:
        return []
    counts: dict[str, int] = {}
    for r in residuals:
        category = _residual_category(str(r.get("reason") or ""))
        counts[category] = counts.get(category, 0) + 1
    if len(counts) == 1:
        category, n = next(iter(counts.items()))
        return [f"{n} access pattern{'s' if n != 1 else ''}: {category}"]
    parts = ", ".join(
        f"{n} {category}" for category, n in sorted(counts.items(), key=lambda kv: -kv[1])
    )
    return [f"{len(residuals)} access patterns need judgment: {parts}"]


def _default_trade_off(base: AuroraDesignBase, n_tables: int) -> dict:
    strategy = base.migration_strategy
    how = (
        "carried over 1:1 from the source"
        if strategy == "carry_over"
        else "translated deterministically from the source types"
    )
    return {
        "description": (
            f"The {n_tables} tables are {how}; the deterministic draft's types, keys, "
            "indexes and foreign keys are kept unless listed as a change."
        ),
        "impact": (
            "The relational model the application already uses stays as it is, so the "
            "migration risk sits in data movement and cut-over rather than in a redesign."
        ),
        "engine": base.engine,
    }


def merge_design_delta(
    base: AuroraDesignBase, delta: dict | AuroraDesignDeltaContract, *, strict: bool = True
) -> MergeResult:
    """Apply ``delta`` to the draft built from ``base``.

    Strict (``--finalize``): any merge error returns ``output=None`` with the
    errors, so the caller fixes the delta. Lenient (Bedrock, after its
    correction attempt): invalid entries are skipped and the errors are
    recorded as ``validation_failures`` with ``validation_passed=false``.
    Warnings (a type rule that matched nothing, residuals left unresolved)
    never fail the merge.
    """
    if isinstance(delta, AuroraDesignDeltaContract):
        contract = delta
    else:
        try:
            contract = AuroraDesignDeltaContract.model_validate(delta)
        except ValidationError as exc:
            return MergeResult(None, [f"Invalid design delta: {exc}"], {})

    errors: list[str] = []
    warnings: list[str] = []
    tables = _Tables.of(base)
    seen: set[str] = set()
    for i, change in enumerate(contract.tables):
        table = tables.find(change.table_name)
        key = table.table_name if table is not None else change.table_name
        schema = _schema_of(change.table_name)
        if table is None:
            errors.append(
                f"tables[{i}]: "
                + _unknown_table(
                    change.table_name, tables, " (use a table_name from the design view)"
                )
            )
        elif schema is not None and schema not in {
            s.lower() for s in (table.schema_name, base.source_database) if s
        }:
            errors.append(
                f"tables[{i}]: schema prefix in '{change.table_name}' does not match table "
                f"'{table.table_name}'"
            )
        elif key in seen:
            errors.append(f"tables[{i}]: table '{change.table_name}' is listed more than once")
        seen.add(key)

    draft = base.generate()
    overrides, by_rule = _type_overrides(base, contract, tables, draft.residuals, errors, warnings)
    ddl = base.generate(overrides)
    table_ddls = {t.table_name: t for t in ddl.tables}
    draft_indexes = {t.table_name: draft_index_names(t, base.dialect) for t in tables.named}
    index_counts = _apply_index_changes(
        base, contract, tables, table_ddls, draft_indexes, errors, ddl.index_notes
    )

    for note in ddl.index_notes:
        warnings.append(f"index {note['table']}.{note['index']}: {note['reason']}")
    defaults = [n for n in ddl.column_notes if n["kind"] == "default"]
    if defaults:
        sample = ", ".join(f"{n['table']}.{n['column']}" for n in defaults[:5])
        warnings.append(
            f"{len(defaults)} column(s) have a source DEFAULT the draft dropped or turned "
            f"into an identity column (e.g. {sample}); review them in the draft's column_notes"
        )
    if ddl.residuals:
        sample = ", ".join(f"{r['table']}.{r['column']}" for r in ddl.residuals[:5])
        warnings.append(
            f"{len(ddl.residuals)} residual column(s) keep the draft's fallback type "
            f"(e.g. {sample}); add type_rules for their source types to resolve them"
        )

    summary = {
        "tables_changed": len(
            {
                found.table_name
                for c in contract.tables
                if c.add_indexes or c.modify_indexes or c.remove_indexes or c.column_types
                if (found := tables.find(c.table_name)) is not None
            }
        ),
        "column_types_set": len(overrides) - by_rule,
        "residuals_resolved_by_rule": by_rule,
        "residuals_unresolved": len(ddl.residuals),
        "indexes_added": index_counts["added"],
        "indexes_modified": index_counts["modified"],
        "indexes_removed": index_counts["removed"],
    }
    if errors and strict:
        return MergeResult(None, errors, summary, warnings)

    trade_offs = [t.model_dump(mode="json") for t in contract.trade_offs] or [
        _default_trade_off(base, len(base.tables))
    ]
    # Derived here rather than taken from the delta, for the same reason
    # ``generated_ddl`` is: the model does not author it. The delta contract
    # declares no access patterns at all, so there is no path by which a
    # measured ``design_rps`` can be re-authored - GitHub #136 is that field
    # going wrong on the DocumentDB path, where the model does author it.
    #
    # Derived from ``base.tables``, which is the source index set the observed
    # queries actually ran against, so ``index_used`` describes the measured
    # baseline; a pattern whose index the draft does not create (not carried
    # onto this engine, or removed by the delta) needs judgment (#1597).
    # Index add/modify entries in the delta are deliberately not
    # reflected: re-deriving against the post-merge index set would describe the
    # proposed design instead, which is a different (and also useful) question,
    # but it needs an _Index -> AgentTable adaptation that belongs in its own
    # change rather than riding along here.
    dropped = frozenset(
        (t.table_name, i.index_name)
        for t in tables.named
        for i in secondary_indexes(t)
        if i.index_name not in draft_indexes[t.table_name]
    )
    derived = derive_access_patterns(
        base.queries,
        base.tables,
        base.engine,
        base.source_engine,
        dropped,
        base.qualified,
        source_database=base.source_database,
    )
    warnings.extend(_access_pattern_residual_warnings(derived.residuals))
    left_out = _left_out_notes(base)
    output = {
        "contract_version": _OUTPUT_CONTRACTS[base.engine].model_fields["contract_version"].default,
        "job_id": base.job_id,
        "source_database": base.source_database,
        "target_engine": base.engine,
        "migration_strategy": base.migration_strategy,
        "table_definitions": [
            {
                "table_name": t.table_name,
                # The schema it is created in, only in a design of several schemas (#2455).
                **({"schema_name": t_ddl.schema_name} if t_ddl.schema_name else {}),
                "columns": [
                    {
                        "name": c.name,
                        "aurora_type": c.aurora_type,
                        "source_type": c.source_type,
                        "script_derived": c.script_derived,
                        "needs_judgment": c.needs_judgment,
                    }
                    for c in t_ddl.columns
                ],
                "primary_key": list(t.primary_key or []),
                # A comment standing in for an index the key limit rules out stays in
                # generated_ddl only, so no deliverable counts an index that is not created.
                "indexes": [i for i in t_ddl.index_sql if not i.startswith("--")],
                "foreign_keys": list(t_ddl.fk_sql),
                # The table's own cut of generated_ddl, so no reader has to split the script.
                "ddl_statements": [t_ddl.create_sql, *t_ddl.index_sql, *t_ddl.fk_sql],
            }
            for t, t_ddl in ((t, table_ddls[base.name_of(t)]) for t in base.tables)
        ],
        # #2484: one comment per object the design leaves out, after the last statement.
        "generated_ddl": with_not_created_comments(assemble_full_ddl(ddl.tables), left_out),
        "access_patterns": derived.patterns,
        "app_layer_notes": [n.model_dump(mode="json") for n in contract.app_layer_notes]
        + _time_zone_notes(ddl)
        + _default_notes(ddl)
        + _index_notes(ddl)
        + left_out,
        "optimizations": [o.model_dump(mode="json") for o in contract.optimizations],
        "trade_offs": trade_offs,
        "validation_passed": not errors,
        "validation_failures": [f"Design delta: {e}" for e in errors],
    }
    return MergeResult(output, errors, summary, warnings)


def base_from_outputs(
    engine: str, collector_output: dict, analysis_output: dict
) -> tuple[AuroraDesignBase, AgentCollectorInput, AgentAnalysisInput]:
    """Project raw (assignment-filtered) collector + analysis dicts into a design base.

    The same projection ``scripts/run_schema_design.py`` and the Bedrock agent
    build the draft from, so prepare and ``--finalize`` see the same draft.
    Returns ``(base, agent_collector, agent_analysis)``.
    """
    from src.contracts.analysis_output import AnalysisOutputContract
    from src.contracts.collector_output import CollectorOutputContract
    from src.contracts.schema_design_input import project_schema_design_input
    from src.shared.contract_versioning import load_contract

    collector = load_contract(
        CollectorOutputContract, collector_output, artifact="collector output"
    )
    analysis = load_contract(
        AnalysisOutputContract, analysis_output, artifact=f"analysis-{engine} output"
    )
    agent_collector, agent_analysis, _ = project_schema_design_input(
        collector, analysis, effective_architecture=analysis_output.get("effective_architecture")
    )
    base = AuroraDesignBase.from_inputs(engine, agent_collector, collector_output)
    return base, agent_collector, agent_analysis


def _same_type(a: str, b: str) -> bool:
    return " ".join(a.lower().split()) == " ".join(b.lower().split())


def full_contract_to_delta(
    base: AuroraDesignBase, response: dict, warnings: list[str] | None = None
) -> tuple[dict | None, list[str]]:
    """Convert a legacy full-contract response into a delta (issue #273, B1).

    Nothing in a full contract is trusted as DDL. Its ``generated_ddl``,
    ``foreign_keys`` and ``primary_key`` are ignored (the merge regenerates
    them from the draft); each column whose ``aurora_type`` differs from the
    draft becomes a ``column_types`` change (validated by the type grammar
    when the delta is validated); each index statement is parsed with
    ``parse_index_statement`` and becomes an add (new name) or a modify
    (draft name, different definition). Draft indexes the response does not
    list are kept. Unknown tables or columns, and indexes that do not parse,
    are errors, never dropped. Returns ``(delta, errors)``; conversions worth
    a look (a modify that names no method for a draft index that has one, or
    drops a draft partial-index predicate since the legacy grammar has no
    WHERE) are appended to ``warnings``.
    """
    errors: list[str] = []
    found = _Tables.of(base)
    draft = base.generate()
    draft_types = {(t.table_name, c.name): c.aurora_type for t in draft.tables for c in t.columns}
    draft_indexes = {
        t.table_name: dict(zip(draft_index_names(src, base.dialect), t.index_sql, strict=True))
        for src, t in zip(base.tables, draft.tables, strict=True)
    }
    # The USING method each draft index renders (None: a B-tree). Only an Aurora PostgreSQL
    # delta can name a method to keep, so only there does leaving it out warn.
    draft_methods = {
        (t.table_name, i.index_name): rendered_method(i, base.dialect, t.columns)
        for src, t in zip(base.tables, draft.tables, strict=True)
        for i in carried_indexes(src, base.dialect)
        if base.dialect is POSTGRES
    }
    tables: list[dict] = []
    for i, table_def in enumerate(response.get("table_definitions") or []):
        name = str((table_def or {}).get("table_name", ""))
        schema_name = (table_def or {}).get("schema_name")
        table = found.find(f"{schema_name}.{name}" if schema_name else name)
        if table is None:
            errors.append(f"table_definitions[{i}]: {_unknown_table(name, found)}")
            continue
        ref = found.refs[table.table_name]
        change: dict = {"table_name": table.table_name, "column_types": [], "add_indexes": []}
        change["modify_indexes"] = []
        columns = [c.column_name for c in table.columns]
        for j, col in enumerate(table_def.get("columns") or []):
            column = resolve_column(columns, str(col.get("name", "")))
            if column is None:
                errors.append(
                    f"table_definitions[{i}].columns[{j}]: table '{table.table_name}' has no "
                    f"column '{col.get('name')}'"
                )
                continue
            aurora_type = str(col.get("aurora_type", ""))
            if not _same_type(aurora_type, draft_types[(table.table_name, column)]):
                change["column_types"].append({"column": column, "aurora_type": aurora_type})
        existing = draft_indexes[table.table_name]
        draft_statements = set(existing.values())
        for j, statement in enumerate(table_def.get("indexes") or []):
            where = f"table_definitions[{i}].indexes[{j}]"
            if str(statement) in draft_statements:
                continue  # the draft's own rendering (incl. partial indexes): unchanged
            try:
                parsed = parse_index_statement(str(statement))
            except SqlFragmentError as exc:
                errors.append(f"{where}: {exc}")
                continue
            if parsed.method is not None and parsed.method not in INDEX_METHODS:
                errors.append(f"{where}: unknown index method '{parsed.method}'")
                continue
            entry = {
                "index_name": parsed.index_name,
                "columns": parsed.columns,
                "unique": parsed.unique,
            }
            if parsed.method:
                entry["method"] = parsed.method
            spelled = f"{parsed.schema}.{parsed.table}" if parsed.schema else parsed.table
            if found.find(spelled) is not table:
                errors.append(
                    f"{where}: index '{parsed.index_name}' is not ON '{table.table_name}'"
                )
                continue
            draft_name = _lookup(existing, parsed.index_name)
            if draft_name is None:
                change["add_indexes"].append(entry)
            elif parsed.index_name and existing[draft_name] != _render(
                base,
                ref,
                _Index(parsed.index_name, parsed.columns, parsed.unique, parsed.method, [], None),
            ):
                change["modify_indexes"].append({**entry, "index_name": draft_name})
                if " WHERE " in existing[draft_name] and warnings is not None:
                    warnings.append(
                        f"{where}: index '{draft_name}' on '{table.table_name}' is redefined "
                        "without the draft's partial-index predicate (WHERE ...), so it now "
                        "covers every row; write a delta with modify_indexes and 'where' to "
                        "keep it"
                    )
                draft_method = draft_methods.get((table.table_name, draft_name))
                if draft_method and parsed.method is None and warnings is not None:
                    warnings.append(
                        f"{where}: index '{draft_name}' on '{table.table_name}' is redefined "
                        f"without the draft's USING {draft_method}, so it is now a B-tree; "
                        "write a delta with modify_indexes and 'method' to keep it"
                    )
        if change["column_types"] or change["add_indexes"] or change["modify_indexes"]:
            tables.append(change)  # only tables that differ from the draft are changes
    if errors:
        return None, errors
    delta = {
        "delta_version": "1.0",
        "tables": tables,
        "optimizations": response.get("optimizations") or [],
        "app_layer_notes": response.get("app_layer_notes") or [],
        "trade_offs": response.get("trade_offs") or [],
    }
    return delta, []
