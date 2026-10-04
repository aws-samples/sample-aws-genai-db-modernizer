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

Nothing from the delta reaches DDL unparsed: types pass the contract's strict
type grammar, and indexes are rendered from structured parts with quoted
identifiers (``ddl_generator.render_index``). See ``sql_safety``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from pydantic import ValidationError

from src.contracts.aurora_design_delta import (
    AuroraDesignDeltaContract,
    IndexSpec,
    TypeRule,
    is_enum_type,
)
from src.contracts.schema_design_input import (
    AgentAnalysisInput,
    AgentCollectorInput,
    AgentTable,
)
from src.tools.schema.aurora_common.ddl_generator import (
    MYSQL,
    POSTGRES,
    DdlResult,
    Dialect,
    TableDDL,
    TypeOverride,
    TypeOverrides,
    assemble_full_ddl,
    generate_mysql_ddl,
    generate_pg_ddl,
    render_index,
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


def _table_key(name: str) -> str:
    """``discourse.users``, ``"users"`` and ``USERS`` all match draft table ``users``."""
    cleaned = "".join(c for c in name if c not in '`"[]').strip().lower()
    return cleaned.rsplit(".", 1)[-1].strip()


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
        return cls(
            engine=engine,
            job_id=agent_collector.job_id,
            source_database=agent_collector.source_database_name,
            source_engine=agent_collector.source_database_engine,
            tables=list(agent_collector.tables),
            source_data_types=source_data_types(raw_collector or {}),
        )

    def generate(self, type_overrides: TypeOverrides | None = None) -> DdlResult:
        return _GENERATORS[self.engine](self.tables, type_overrides)

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
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return f"sha256:{digest}"


def draft_index_names(table: AgentTable) -> list[str]:
    """Names of the draft's secondary indexes, in ``TableDDL.index_sql`` order."""
    return [i.index_name for i in table.indexes or [] if not i.is_primary]


def source_data_types(raw_collector: dict) -> dict[tuple[str, str], str]:
    """``(table_name, column_name) -> data_type`` from a raw collector output dict."""
    types: dict[tuple[str, str], str] = {}
    for table in (raw_collector.get("database_schema") or {}).get("tables") or []:
        for col in table.get("columns") or []:
            if col.get("data_type"):
                types[(table.get("table_name", ""), col.get("column_name", ""))] = str(
                    col["data_type"]
                )
    return types


@dataclass
class MergeResult:
    output: dict | None
    errors: list[str]
    summary: dict
    warnings: list[str] = field(default_factory=list)


def _check_dialect_type(engine: str, aurora_type: str, where: str, errors: list[str]) -> bool:
    if is_enum_type(aurora_type) and engine != "aurora_mysql":
        errors.append(
            f"{where}: ENUM/SET literal types are Aurora MySQL only; on Aurora PostgreSQL "
            "use TEXT (or a lookup table) and record the allowed values in a trade-off"
        )
        return False
    if aurora_type.endswith("[]") and engine != "aurora_postgresql":
        errors.append(f"{where}: array types ({aurora_type}) are Aurora PostgreSQL only")
        return False
    return True


def _type_overrides(
    base: AuroraDesignBase,
    delta: AuroraDesignDeltaContract,
    by_key: dict[str, AgentTable],
    residuals: list[dict],
    errors: list[str],
    warnings: list[str],
) -> tuple[TypeOverrides, int]:
    overrides: TypeOverrides = {}
    for i, change in enumerate(delta.tables):
        table = by_key.get(_table_key(change.table_name))
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


def _structured_index(base: AuroraDesignBase, table: AgentTable, entry: IndexSpec | str) -> _Index:
    """Validate one delta index entry against the table and dialect. Raises SqlFragmentError."""
    engine = base.engine
    columns = [c.column_name for c in table.columns]
    if isinstance(entry, str):
        parsed = parse_index_statement(entry)
        if _table_key(parsed.table) != _table_key(table.table_name):
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
        if parsed.method not in (None, "btree", "hash", "gin", "gist", "brin"):
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
    where_sql = (
        render_predicate(spec.where, columns, base.dialect.q) if spec.where is not None else None
    )
    return _Index(
        name=spec.index_name,
        columns=resolved[: len(spec.columns)],
        unique=spec.unique,
        method=spec.method,
        include=resolved[len(spec.columns) :],
        where_sql=where_sql,
    )


def _render(base: AuroraDesignBase, table: AgentTable, index: _Index) -> str:
    return render_index(
        base.dialect,
        table.table_name,
        index.name,
        index.columns,
        unique=index.unique,
        method=index.method,
        include=index.include,
        where_sql=index.where_sql,
    )


def _lookup(current: dict[str, str], name: str) -> str | None:
    if name in current:
        return name
    hits = [n for n in current if n.lower() == name.lower()]
    return hits[0] if len(hits) == 1 else None


def _apply_index_changes(
    base: AuroraDesignBase,
    delta: AuroraDesignDeltaContract,
    by_key: dict[str, AgentTable],
    table_ddls: dict[str, TableDDL],
    errors: list[str],
) -> dict[str, int]:
    counts = {"added": 0, "modified": 0, "removed": 0}
    for i, change in enumerate(delta.tables):
        table = by_key.get(_table_key(change.table_name))
        if table is None:
            continue
        ddl = table_ddls[table.table_name]
        # name -> statement, in draft order (names come from the source, not the model)
        current: dict[str, str] = dict(zip(draft_index_names(table), ddl.index_sql, strict=True))

        for j, name in enumerate(change.remove_indexes):
            existing = _lookup(current, name.strip().strip('"`'))
            if existing is None:
                errors.append(
                    f"tables[{i}].remove_indexes[{j}]: table '{table.table_name}' has no "
                    f"index '{name}'"
                )
                continue
            del current[existing]
            counts["removed"] += 1

        for j, entry in enumerate(change.modify_indexes):
            where = f"tables[{i}].modify_indexes[{j}]"
            try:
                index = _structured_index(base, table, entry)
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
            current[existing] = _render(base, table, index)
            counts["modified"] += 1

        for j, entry in enumerate(change.add_indexes):
            where = f"tables[{i}].add_indexes[{j}]"
            try:
                index = _structured_index(base, table, entry)
            except (SqlFragmentError, ValidationError) as exc:
                errors.append(f"{where}: {exc}")
                continue
            if _lookup(current, index.name) is not None:
                errors.append(
                    f"{where}: index '{index.name}' already exists on '{table.table_name}'"
                )
                continue
            current[index.name] = _render(base, table, index)
            counts["added"] += 1

        ddl.index_sql = list(current.values())
    return counts


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
    by_key = {_table_key(t.table_name): t for t in base.tables}
    seen: set[str] = set()
    for i, change in enumerate(contract.tables):
        key = _table_key(change.table_name)
        table = by_key.get(key)
        schema = _schema_of(change.table_name)
        if table is None:
            errors.append(
                f"tables[{i}]: unknown table '{change.table_name}'; the draft has no such "
                "table (use a table_name from the design view)"
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
    overrides, by_rule = _type_overrides(base, contract, by_key, draft.residuals, errors, warnings)
    ddl = base.generate(overrides)
    table_ddls = {t.table_name: t for t in ddl.tables}
    index_counts = _apply_index_changes(base, contract, by_key, table_ddls, errors)

    if ddl.residuals:
        sample = ", ".join(f"{r['table']}.{r['column']}" for r in ddl.residuals[:5])
        warnings.append(
            f"{len(ddl.residuals)} residual column(s) keep the draft's fallback type "
            f"(e.g. {sample}); add type_rules for their source types to resolve them"
        )

    summary = {
        "tables_changed": len(seen & set(by_key)),
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
    output = {
        "contract_version": "1.0",
        "job_id": base.job_id,
        "source_database": base.source_database,
        "target_engine": base.engine,
        "migration_strategy": base.migration_strategy,
        "table_definitions": [
            {
                "table_name": t.table_name,
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
                "indexes": list(t_ddl.index_sql),
                "foreign_keys": list(t_ddl.fk_sql),
            }
            for t, t_ddl in ((t, table_ddls[t.table_name]) for t in base.tables)
        ],
        "generated_ddl": assemble_full_ddl(ddl.tables),
        "app_layer_notes": [n.model_dump(mode="json") for n in contract.app_layer_notes],
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

    collector = CollectorOutputContract.model_validate(collector_output)
    analysis = AnalysisOutputContract.model_validate(analysis_output)
    agent_collector, agent_analysis, _ = project_schema_design_input(collector, analysis)
    base = AuroraDesignBase.from_inputs(engine, agent_collector, collector_output)
    return base, agent_collector, agent_analysis
