# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Load a job's saved artifacts once into an in-process DuckDB database (#2143).

Each table holds one kind of saved fact, flattened from the job folder as the pipeline
wrote it: every assignment version (``assignment/v<N>/assignment.json``), every schema
design (``schema-<engine>/v<N>/schema_output.json``) and design split
(``groups_manifest.json`` and its part inputs), and every synthesis ``report.json``. The
loader only reads and flattens; it counts nothing and decides nothing, so the checks in
:mod:`src.tools.invariants.checks` are the only place a rule is applied.

What the loader takes from the product code is the reading of saved files, never a
routing decision: routing's own placement units (``assignment_placement_groups``), the
tables a statement writes (``write_groups.written_tables``), the statements that follow
their routine and the statements with no source table. Those are the same readers the
pipeline's own guards use (``split_invariant``, ``design_coverage``).

Older artifacts load (CONTRIBUTING.md, "Renaming an id or an enum value"): a file kept
compressed (``<name>.gz``, as the saved replay fixtures keep them) is read decompressed;
an engine id an older artifact spelt differently is mapped with ``canonical_engine_id``;
an assignment written before write groups (no ``write_groups``) or routine units (no
``routine_units``) loads with that fact absent, the same way the product reads it, and
:func:`load_job` lists each such gap in ``JobTables.legacy``. Only an assignment saved
before the move ledger (no ``contract_version``, or one below 1.15) may lack ``moves``;
a versioned one without them is loaded as having recorded none, which the ledger check
reports (the review of #2370). A version saved before contract 1.26 is marked
``units_legacy``, as ``assignment_validator.saved_before_units`` decides, so a split it
already had is a warning, as the validator's legacy baseline makes it.
"""

from __future__ import annotations

import gzip
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

_VERSION_DIR = re.compile(r"^v(\d+)$")

# One table per saved fact: name -> column definitions. The checks read only these.
SCHEMA: dict[str, str] = {
    "versions": (
        "version INTEGER, previous_version INTEGER, has_moves BOOLEAN, contract VARCHAR, "
        "ledger_legacy BOOLEAN, units_legacy BOOLEAN"
    ),
    "query_assignments": (
        "version INTEGER, query_id VARCHAR, engine VARCHAR, in_scope BOOLEAN, "
        "tied VARCHAR, cache_engine VARCHAR, customer_override BOOLEAN"
    ),
    "unservable": "version INTEGER, query_id VARCHAR",
    "moves": "version INTEGER, seq INTEGER, query_id VARCHAR, to_engine VARCHAR, tied VARCHAR",
    "co_dependency": "version INTEGER, group_no INTEGER, query_id VARCHAR",
    "write_groups": "version INTEGER, table_name VARCHAR, query_id VARCHAR",
    "routine_units": "version INTEGER, routine_id VARCHAR, query_id VARCHAR",
    "placement_units": "version INTEGER, unit_id VARCHAR, query_id VARCHAR",
    "table_roles": (
        "version INTEGER, origin VARCHAR, table_id VARCHAR, role VARCHAR, engine VARCHAR"
    ),
    "statements": "query_id VARCHAR, routine_follower BOOLEAN, has_source_table BOOLEAN",
    "statement_writes": "query_id VARCHAR, table_name VARCHAR",
    "cache_engines": "engine VARCHAR",
    "coverage_engines": "engine VARCHAR",
    "moving_roles": "role VARCHAR",
    "designs": "engine VARCHAR, version INTEGER",
    "design_names": "engine VARCHAR, version INTEGER, query_id VARCHAR",
    "design_excluded": "engine VARCHAR, version INTEGER, query_id VARCHAR",
    "splits": "engine VARCHAR, version INTEGER",
    "split_parts": "engine VARCHAR, version INTEGER, part INTEGER, query_id VARCHAR",
    "split_owners": "engine VARCHAR, version INTEGER, part INTEGER, table_name VARCHAR",
    "unit_cuts": "engine VARCHAR, version INTEGER, unit_id VARCHAR, part INTEGER",
    "unit_cut_statements": "engine VARCHAR, version INTEGER, unit_id VARCHAR, query_id VARCHAR",
    "reports": "version INTEGER, doc JSON",
    "report_engines": (
        "version INTEGER, engine VARCHAR, role VARCHAR, monthly_cost_usd DOUBLE, "
        "workload_percent DOUBLE"
    ),
    "report_ranking": "version INTEGER, engine VARCHAR, in_scope_queries INTEGER",
    "report_open_risks": "version INTEGER, severity VARCHAR, risk_count INTEGER",
}


#: The contract version every assignment has recorded ``moves`` since (#616): a file
#: with no ``contract_version``, or a lower one, predates the move ledger.
LEDGER_SINCE = (1, 15)


def _before(contract: str | None, since: tuple[int, int]) -> bool:
    """Whether ``contract`` (``MAJOR.MINOR``) is absent, unreadable or below ``since``."""
    try:
        major, minor = (int(p) for p in str(contract or "").split(".", 1))
    except ValueError:
        return True
    return (major, minor) < since


@dataclass
class JobTables:
    """The loaded database and what loading it found."""

    con: duckdb.DuckDBPyConnection
    job_dir: Path
    counts: dict[str, int] = field(default_factory=dict)
    legacy: list[str] = field(default_factory=list)


def read_json(path: Path, job_dir: Path) -> Any:
    """``path``'s JSON, or its ``<name>.gz`` form's; None when neither exists.

    Checked as the artifact store checks what it reads (ADR-008 Decision 4,
    ``check_artifact`` on the path's store key ``<db>/<job>/...``): a registered contract
    at an older MAJOR comes back through its adapter, and one this code cannot read
    raises ``ContractVersionError``."""
    from src.shared.contract_versioning import check_artifact

    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        packed = path.with_name(path.name + ".gz")
        if not packed.is_file():
            return None
        data = json.loads(gzip.decompress(packed.read_bytes()).decode("utf-8"))
    key = f"{job_dir.parent.name}/{job_dir.name}/{path.relative_to(job_dir).as_posix()}"
    return check_artifact(key, data)


def _versioned(base: Path, filename: str) -> dict[int, Path]:
    """``{N: base/v<N>/filename}`` for each version folder holding the file (or its .gz)."""
    found: dict[int, Path] = {}
    if not base.is_dir():
        return found
    for child in base.iterdir():
        m = _VERSION_DIR.match(child.name)
        target = child / filename
        if m and (target.is_file() or target.with_name(filename + ".gz").is_file()):
            found[int(m.group(1))] = target
    return dict(sorted(found.items()))


def _engine(value: Any) -> str | None:
    from src.shared.engine_names import canonical_engine_id

    if value is None or value == "":
        return None
    return canonical_engine_id(str(value))


def _tied(values: Iterable[Any] | None) -> str:
    return ",".join(sorted({e for e in (_engine(v) for v in values or []) if e}))


def _qid(value: Any) -> str:
    return str(value.get("query_id") if isinstance(value, Mapping) else value)


class _Rows:
    """Rows collected per table, inserted column by column (one statement per table)."""

    def __init__(self) -> None:
        self.rows: dict[str, list[tuple[Any, ...]]] = {name: [] for name in SCHEMA}

    def add(self, table: str, *row: Any) -> None:
        self.rows[table].append(row)

    def extend(self, table: str, rows: Iterable[Sequence[Any]]) -> None:
        self.rows[table].extend(tuple(r) for r in rows)

    def write(self, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        counts: dict[str, int] = {}
        for name, columns in SCHEMA.items():
            # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query,python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- names and types from the fixed SCHEMA, values bound
            con.execute(f"CREATE TABLE {name} ({columns})")
            rows = self.rows[name]
            counts[name] = len(rows)
            if not rows:
                continue
            width = len(rows[0])
            types = [c.strip().split(" ", 1)[1] for c in columns.split(",")]
            # Column-wise unnest: one statement per table, far faster than executemany.
            selects = ", ".join(f"unnest(?::{t}[])" for t in types[:width])
            # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query,python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- names and types from the fixed SCHEMA, values bound
            con.execute(
                f"INSERT INTO {name} SELECT {selects}",
                [list(col) for col in zip(*rows, strict=True)],
            )
        return counts


def _collector(job_dir: Path) -> dict:
    from src.shared.sql_spans import span_settings_of, use_span_settings
    from src.tools.sql.query_tags import without_query_tags

    raw = read_json(job_dir / "collector" / "output.json", job_dir)
    collector = without_query_tags(raw) if isinstance(raw, dict) else {}
    use_span_settings(span_settings_of(collector) if collector else None)
    return collector


def _load_statements(rows: _Rows, collector: Mapping[str, Any]) -> None:
    """Each collected statement's flags and the tables it writes, canonical names."""
    from src.agents.referee.routine_units import RoutineFollowers
    from src.agents.referee.table_resolution import SourceScope, TableNameResolver
    from src.agents.referee.write_groups import written_tables
    from src.agents.schema_design.group_splitter import has_no_source_table
    from src.tools.sql.query_facts import source_engine_of

    patterns = [
        q
        for q in (collector.get("queries") or {}).get("query_patterns") or []
        if isinstance(q, Mapping) and q.get("query_id") is not None
    ]
    if not patterns:
        return
    followers = RoutineFollowers.from_collector(collector)
    resolver = TableNameResolver.from_collector(dict(collector))
    dialect = source_engine_of(collector)
    scope = SourceScope.from_collector(dict(collector))
    for q in patterns:
        qid = str(q["query_id"])
        rows.add("statements", qid, qid in followers, not has_no_source_table(dict(q)))
        for table in dict.fromkeys(written_tables(q, dialect, scope)):
            canon = (resolver.resolve(table) if resolver else None) or table
            rows.add("statement_writes", qid, canon)


def _load_assignment(
    rows: _Rows, version: int, a: Mapping[str, Any], collector: Mapping, legacy: list[str]
) -> None:
    from src.agents.referee.assignment_validator import saved_before_units
    from src.agents.referee.routine_units import records_routine_units, routine_units_of
    from src.agents.referee.write_groups import assignment_placement_groups
    from src.agents.schema_design.placement_units import unit_id_of
    from src.shared.placeholder_tables import saved_write_groups

    prev = a.get("previous_version")
    has_moves = "moves" in a
    contract = str(a["contract_version"]) if a.get("contract_version") else None
    ledger_legacy = _before(contract, LEDGER_SINCE)
    if not has_moves and ledger_legacy:
        legacy.append(f"assignment v{version}: no moves (saved before the move ledger)")
    rows.add(
        "versions",
        version,
        int(prev) if prev is not None else None,
        has_moves,
        contract,
        ledger_legacy,
        saved_before_units(dict(a)),
    )
    for qa in a.get("query_assignments") or []:
        if not isinstance(qa, Mapping):
            continue
        rows.add(
            "query_assignments",
            version,
            _qid(qa),
            _engine(qa.get("assigned_engine")),
            qa.get("in_scope", True) is not False,
            _tied(qa.get("tied_engines")),
            _engine(qa.get("cache_engine")),
            qa.get("customer_override") is True,
        )
    for u in a.get("unservable_queries") or []:
        rows.add("unservable", version, _qid(u))
    for m in a.get("moves") or []:
        if isinstance(m, Mapping):
            rows.add(
                "moves",
                version,
                int(m.get("seq") or 0),
                _qid(m),
                _engine(m.get("to_engine")),
                _tied(m.get("tied_engines")),
            )
    for n, group in enumerate(a.get("co_dependency_groups") or []):
        rows.extend("co_dependency", ((version, n, str(q)) for q in group))
    if "write_groups" not in a:
        legacy.append(f"assignment v{version}: no write_groups (saved before write groups)")
    for table, members in saved_write_groups(dict(a)).items():
        rows.extend("write_groups", ((version, str(table), str(q)) for q in members))
    if not records_routine_units(dict(a)):
        legacy.append(f"assignment v{version}: no routine_units (read from the collector)")
    for routine, members in routine_units_of(dict(a), collector).items():
        rows.extend("routine_units", ((version, str(routine), str(q)) for q in members))
    for unit in assignment_placement_groups(dict(a), collector):
        uid = unit_id_of(unit)
        rows.extend("placement_units", ((version, uid, str(q)) for q in unit))
    for t in a.get("table_assignments") or []:
        if not isinstance(t, Mapping):
            continue
        tid = str(t.get("table_id"))
        for role in ("primary_engine", "source_of_truth"):
            if t.get(role):
                rows.add("table_roles", version, "assignment", tid, role, _engine(t[role]))
        for e in t.get("write_engines") or []:
            rows.add("table_roles", version, "assignment", tid, "write_engine", _engine(e))


def _design_query_ids(engine: str | None, design: Mapping[str, Any]) -> list[str]:
    """The statements a design serves, read as the product's coverage check reads them
    (``scope.covered_query_ids``): its access patterns' ``query_ids`` (DynamoDB,
    OpenSearch) and ``source_query_ids`` (the others), an access pattern marked
    ``in_scope: false`` serving none, and for DocumentDB its collections' indexes'
    ``source_query_ids`` too."""
    ids: list[str] = []
    for ap in design.get("access_patterns") or []:
        if not isinstance(ap, Mapping) or ap.get("in_scope") is False:
            continue
        for key in ("query_ids", "source_query_ids"):
            ids.extend(str(q) for q in ap.get(key) or [])
    if engine == "documentdb":
        for collection in design.get("collections") or []:
            if not isinstance(collection, Mapping):
                continue
            for index in collection.get("indexes") or []:
                if isinstance(index, Mapping):
                    ids.extend(str(q) for q in index.get("source_query_ids") or [])
    return list(dict.fromkeys(ids))


def _load_designs(rows: _Rows, job_dir: Path) -> None:
    from src.agents.schema_design.pattern_input import input_query_ids

    for engine_dir in sorted(job_dir.glob("schema-*")):
        engine = _engine(engine_dir.name.removeprefix("schema-"))
        for version, path in _versioned(engine_dir, "schema_output.json").items():
            design = read_json(path, job_dir)
            # A skip record (#2154) or a placeholder has no access patterns: no design.
            if (
                isinstance(design, Mapping)
                and design.get("status") != "skipped"
                and "access_patterns" in design
            ):
                rows.add("designs", engine, version)
                for qid in _design_query_ids(engine, design):
                    rows.add("design_names", engine, version, qid)
                for q in design.get("excluded_queries") or []:
                    rows.add("design_excluded", engine, version, _qid(q))
        for version, path in _versioned(engine_dir, "groups_manifest.json").items():
            manifest = read_json(path, job_dir)
            if not isinstance(manifest, Mapping):
                continue
            rows.add("splits", engine, version)
            for group in manifest.get("groups") or []:
                part = int(group["group_index"])
                data = read_json(path.parent / str(group["input_file"]), job_dir) or {}
                for qid in input_query_ids(data.get("collector_output") or {}):
                    rows.add("split_parts", engine, version, part, qid)
                for table in group.get("primary_tables") or []:
                    rows.add("split_owners", engine, version, part, str(table))
            for cut in manifest.get("unit_cuts") or []:
                if not isinstance(cut, Mapping):
                    continue
                uid = str(cut.get("unit_id"))
                for p in cut.get("parts") or []:
                    rows.add("unit_cuts", engine, version, uid, int(p))
                for q in cut.get("query_ids") or []:
                    rows.add("unit_cut_statements", engine, version, uid, str(q))


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _load_reports(rows: _Rows, job_dir: Path, legacy: list[str]) -> None:
    for version, path in _versioned(job_dir / "synthesis", "report.json").items():
        report = read_json(path, job_dir)
        if not isinstance(report, Mapping):
            continue
        rows.add("reports", version, json.dumps(report))
        facts = report.get("facts")
        if not isinstance(facts, Mapping):
            legacy.append(f"synthesis v{version}: no facts (saved before the facts layer)")
            continue
        for e in facts.get("engines") or []:
            if isinstance(e, Mapping):
                rows.add(
                    "report_engines",
                    version,
                    _engine(e.get("engine")),
                    str(e.get("role") or ""),
                    _number(e.get("monthly_cost_usd")),
                    _number(e.get("workload_percent")),
                )
        for r in report.get("ranking") or []:
            if isinstance(r, Mapping) and r.get("target"):
                n = r.get("assigned_queries_in_scope", r.get("assigned_queries"))
                if _number(n) is not None:
                    rows.add("report_ranking", version, _engine(r["target"]), int(n))
        for severity, n in (facts.get("open_risks_by_severity") or {}).items():
            if _number(n) is not None:
                rows.add("report_open_risks", version, str(severity), int(n))
        for m in report.get("table_mappings") or []:
            if not isinstance(m, Mapping):
                continue
            for role in ("owner", "source_of_truth"):
                if m.get(role):
                    rows.add(
                        "table_roles",
                        version,
                        "report",
                        str(m.get("source_table")),
                        role,
                        _engine(m[role]),
                    )


def _load_constants(rows: _Rows) -> None:
    from src.agents.schema_design.design_coverage import COVERAGE_ENGINES
    from src.shared.cache_engines import CACHE_ENGINES
    from src.shared.report_facts import MOVING_ROLES

    rows.extend("cache_engines", ((e,) for e in sorted(CACHE_ENGINES)))
    rows.extend("coverage_engines", ((e,) for e in sorted(COVERAGE_ENGINES)))
    rows.extend("moving_roles", ((r,) for r in MOVING_ROLES))


def load_job(job_dir: Path, database: str = ":memory:") -> JobTables:
    """Read the job folder ``job_dir`` into a DuckDB database (in memory, or the file
    ``database``, which is replaced) and return it with what loading found."""
    job_dir = Path(job_dir)
    assignments = _versioned(job_dir / "assignment", "assignment.json")
    if not assignments:
        raise FileNotFoundError(f"{job_dir}: no assignment/v<N>/assignment.json")
    if database != ":memory:":
        Path(database).unlink(missing_ok=True)
    con = duckdb.connect(
        database,
        config={"autoinstall_known_extensions": False, "autoload_known_extensions": False},
    )
    rows = _Rows()
    legacy: list[str] = []
    collector = _collector(job_dir)
    if not collector:
        legacy.append("collector: no output.json (statement checks have nothing to read)")
    _load_constants(rows)
    _load_statements(rows, collector)
    for version, path in assignments.items():
        a = read_json(path, job_dir)
        if isinstance(a, Mapping):
            _load_assignment(rows, version, a, collector, legacy)
    _load_designs(rows, job_dir)
    _load_reports(rows, job_dir, legacy)
    counts = rows.write(con)
    return JobTables(con=con, job_dir=job_dir, counts=counts, legacy=legacy)
