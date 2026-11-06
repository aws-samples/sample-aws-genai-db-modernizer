# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Collector text never reaches DDL unchecked (AGENTS.md, "Untrusted text is data", #1860).

The sentence says generated DDL types pass an allowlist (``validate_aurora_type``), so
collector text never reaches DDL unchecked. These tests prove it for every path that
writes Aurora DDL:

- every column type the DDL generator writes is the allowlist's answer, for collector
  input of any shape, and a type source that returns something else (the source-type
  map, either normalized-type map, a residual fallback) is refused, not written;
- ``ColumnDDL`` is built in one place, which runs the allowlist;
- outside an allowlisted type, collector text appears in generated DDL only inside a
  quoted identifier, a string literal or a ``--`` comment it cannot end: a sentinel put
  into every collector field never shows in the remaining SQL code, in the generator's
  DDL and in the data movement step's ``step-1.ddl.sql``;
- every module under ``src/`` that formats DDL from values is in a reviewed list, so a
  new DDL writer fails here until it gets the same proof.
"""

from __future__ import annotations

import ast
import dataclasses
import re
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from src.contracts.aurora_design_delta import validate_aurora_type
from src.contracts.schema_design_input import (
    AgentColumn,
    AgentForeignKey,
    AgentIndex,
    AgentTable,
    ForeignKeyAction,
    IndexType,
    NormalizedDataType,
)
from src.tools.data_movement.dms_step import (
    DDL_FILE,
    DmsStepBundle,
    DmsStepError,
    bundle_inputs,
    render_ddl,
    render_files,
)
from src.tools.schema.aurora_common import ddl_generator
from src.tools.schema.aurora_common.ddl_generator import generate_mysql_ddl, generate_pg_ddl
from src.tools.schema.aurora_common.type_map import TypeResolution
from tests.hypothesis_examples import scaled
from tests.unit.tools.data_movement import dms_step_fixtures
from tests.unit.tools.data_movement.dms_step_fixtures import make_bundle

REPO = Path(__file__).resolve().parents[5]
SENTINEL = "zq9x"
HOSTILE_TYPE = "BIGINT); DROP TABLE users; --"
GENERATORS = {"aurora_postgresql": generate_pg_ddl, "aurora_mysql": generate_mysql_ddl}

# SQL metacharacters a collector string could carry; every generated string ends with
# the sentinel, so a field that reaches SQL code unquoted shows it there.
_PIECES = ['"', "'", "`", ";", "--", "\n", "\r", "\\", "(", ")", ",", " ", "/*", "*/", "$$",
           "E'", "DROP TABLE t", "NULL", "1", "id", SENTINEL]  # fmt: skip
_hostile = st.lists(st.sampled_from(_PIECES), max_size=6).map(lambda p: "".join(p) + SENTINEL)
_REAL_TYPES = ["varchar(20)", "integer", "numeric(10,2)", "text", "timestamptz", "int unsigned",
               "enum('a','b')", "bigint", "decimal(70,40)", "jsonb", "tinyint(1)", "bit(3)"]  # fmt: skip
_data_type = st.one_of(
    st.none(),
    st.sampled_from(_REAL_TYPES),
    _hostile,
    st.sampled_from(_REAL_TYPES).flatmap(lambda t: _hostile.map(lambda h: f"{t} {h}")),
)
_small_int = st.one_of(st.none(), st.integers(min_value=-5, max_value=100_000))


@st.composite
def _tables(draw: st.DrawFn) -> list[AgentTable]:
    names = [draw(_hostile) + str(i) for i in range(draw(st.integers(1, 4)))]
    columns = [
        AgentColumn(
            column_name=name,
            data_type=draw(_data_type),
            normalized_data_type=draw(st.one_of(st.none(), st.sampled_from(NormalizedDataType))),
            max_length=draw(_small_int),
            numeric_precision=draw(_small_int),
            numeric_scale=draw(_small_int),
            datetime_precision=draw(st.one_of(st.none(), st.integers(0, 9))),
            nullable=draw(st.booleans()),
            default_value=draw(st.one_of(st.none(), _hostile, st.sampled_from(
                [f"'{SENTINEL}'", f"{SENTINEL}()", f"nextval('{SENTINEL}'::regclass)", "now()", "0"]))),
            is_auto_increment=draw(st.booleans()),
        )  # fmt: skip
        for name in names
    ]
    some = st.lists(st.sampled_from(names), min_size=1, max_size=3, unique=True)
    indexes = [
        AgentIndex(
            index_name=draw(_hostile),
            columns=draw(some),
            is_unique=draw(st.booleans()),
            index_type=draw(st.one_of(st.none(), st.sampled_from(IndexType))),
            predicate=draw(st.one_of(st.none(), _hostile, some.map(
                lambda c: f"{c[0]} IS NOT NULL AND {c[-1]} = '{SENTINEL}'"))),
            has_expressions=draw(st.booleans()),
            index_expressions=draw(st.one_of(st.none(), _hostile)),
            index_def=draw(st.one_of(st.none(), _hostile)),
        )  # fmt: skip
        for _ in range(draw(st.integers(0, 2)))
    ]
    fks = [
        AgentForeignKey(
            constraint_name=draw(_hostile),
            columns=draw(some),
            referenced_table=draw(_hostile),
            referenced_columns=draw(st.lists(_hostile, min_size=1, max_size=2)),
            on_delete=draw(st.one_of(st.none(), st.sampled_from(ForeignKeyAction))),
        )
        for _ in range(draw(st.integers(0, 1)))
    ]
    table_name = draw(_hostile)
    return [
        AgentTable(
            table_id=f"s.{table_name}",
            table_name=table_name,
            row_count=None,
            columns=columns,
            indexes=indexes or None,
            primary_key=draw(st.one_of(st.none(), some)),
            foreign_keys=fks or None,
        )
    ]


def sql_code(sql: str, engine: str) -> str:
    """``sql`` with its quoted identifiers, string literals and ``--`` comments taken out:
    what the server reads as SQL code. An unterminated quote fails the test, since the
    rest of the file would be read as part of it."""
    ident = '"' if engine == "aurora_postgresql" else "`"
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        if sql.startswith("--", i):
            m = re.compile(r"[\n\r]").search(sql, i)
            i = n if m is None else m.start()
            continue
        c = sql[i]
        escape_string = engine == "aurora_mysql" or (
            c in "eE" and sql[i + 1 : i + 2] == "'" and not (i and (sql[i - 1].isalnum() or sql[i - 1] == "_"))
        )  # fmt: skip
        if c in "eE" and engine == "aurora_postgresql" and escape_string:
            i += 1
            c = "'"
        if c == ident or c == "'":
            backslashes = c == "'" and escape_string
            j = i + 1
            while True:
                if j >= n:
                    raise AssertionError(f"unterminated {c} at {i}: {sql[i:i + 80]!r}")
                if backslashes and sql[j] == "\\":
                    j += 2
                    continue
                if sql[j] == c:
                    if sql[j + 1 : j + 2] == c:
                        j += 2
                        continue
                    break
                j += 1
            out.append(" ")
            i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


@pytest.mark.passes_on_main
def test_the_code_reader_sees_through_quotes_and_comments_only():
    """The helper the property tests rely on: a sentinel inside a quoted name, a
    literal or a comment is not code; one outside, or after a line break ends the
    comment, is."""
    pg, my = "aurora_postgresql", "aurora_mysql"
    assert SENTINEL not in sql_code(
        f"CREATE TABLE \"a\"\"{SENTINEL}\" (x TEXT DEFAULT '{SENTINEL}''');", pg
    )
    assert SENTINEL not in sql_code(f"x DEFAULT E'\\'{SENTINEL}' -- {SENTINEL}", pg)
    assert SENTINEL not in sql_code(f"`a``{SENTINEL}` DEFAULT 'it\\'s {SENTINEL}'", my)
    assert SENTINEL in sql_code(f'"a"{SENTINEL}', pg)
    assert SENTINEL in sql_code(f"-- {SENTINEL}\n{SENTINEL}", pg)
    assert SENTINEL in sql_code(f"-- x\r{SENTINEL}", my)
    with pytest.raises(AssertionError, match="unterminated"):
        sql_code(f'"{SENTINEL}', pg)


@pytest.mark.passes_on_main
@settings(max_examples=scaled(200), deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    tables=_tables(),
    engine=st.sampled_from(sorted(GENERATORS)),
    source_engine=st.sampled_from(["", "postgresql", "mysql", "oracle", "sqlserver"]),
)
def test_every_ddl_column_type_is_the_allowlists_answer(tables, engine, source_engine):
    """Whatever the collector says a column is, the type the DDL declares for it is one
    ``validate_aurora_type`` accepts for the target engine, unchanged, and it is the type
    the column's definition in ``CREATE TABLE`` carries."""
    result = GENERATORS[engine](tables, source_engine=source_engine)
    for table in result.tables:
        for column in table.columns:
            assert validate_aurora_type(column.aurora_type, engine) == column.aurora_type
            assert column.fragment in table.create_sql
            quoted = ddl_generator.POSTGRES.q(column.name) if engine == "aurora_postgresql" else (
                ddl_generator.MYSQL.q(column.name))  # fmt: skip
            assert column.fragment.startswith(f"{quoted} {column.aurora_type}")


@pytest.mark.passes_on_main
@settings(max_examples=scaled(200), deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    tables=_tables(),
    engine=st.sampled_from(sorted(GENERATORS)),
    source_engine=st.sampled_from(["", "postgresql", "mysql", "oracle"]),
)
def test_collector_text_in_generated_ddl_is_only_quoted_or_commented(tables, engine, source_engine):
    """Every collector string field (names, source types, defaults, predicates, index
    expressions, foreign keys) carries the sentinel; none of it is SQL code in the DDL."""
    ddl = GENERATORS[engine](tables, source_engine=source_engine).full_ddl
    assert SENTINEL not in sql_code(ddl, engine).lower()


def _fake_resolution(needs_judgment: bool):
    return lambda *a, **k: TypeResolution(
        aurora_type=HOSTILE_TYPE, needs_judgment=needs_judgment, reason="x"
    )


def _column(**kw) -> AgentTable:
    return AgentTable(
        table_id="s.t",
        table_name="t",
        row_count=None,
        columns=[AgentColumn(column_name="c", nullable=True, **kw)],
    )


@pytest.mark.parametrize("needs_judgment", [False, True], ids=["confident", "residual"])
@pytest.mark.parametrize("engine", sorted(GENERATORS))
@pytest.mark.parametrize("source", ["source_type_map", "normalized_type_map"])
def test_a_type_source_that_returns_an_unchecked_type_is_refused(
    monkeypatch, source, engine, needs_judgment
):
    """Each place a column type comes from, patched to answer a type off the allowlist:
    the generator refuses it rather than write it. A residual (``needs_judgment``) is a
    fallback type too, and is checked the same way."""
    fake = _fake_resolution(needs_judgment)
    if source == "source_type_map":
        monkeypatch.setattr(ddl_generator, "resolve_source_type", fake)
        table = _column(data_type="integer", normalized_data_type=NormalizedDataType.integer)
        source_engine = "postgresql"
    else:
        name = "POSTGRES" if engine == "aurora_postgresql" else "MYSQL"
        dialect = getattr(ddl_generator, name)
        monkeypatch.setattr(ddl_generator, name, dataclasses.replace(dialect, resolve_type=fake))
        table = _column(normalized_data_type=NormalizedDataType.integer)
        source_engine = "oracle"
    with pytest.raises(ValueError, match="is not a plain SQL type|is not an aurora"):
        GENERATORS[engine]([table], source_engine=source_engine)


@pytest.mark.parametrize("engine", sorted(GENERATORS))
def test_a_type_valid_only_on_the_other_engine_is_refused(monkeypatch, engine):
    """The check is per engine: a MySQL-only type is not written into PostgreSQL DDL, nor
    a PostgreSQL array into MySQL DDL."""
    other = "INT UNSIGNED" if engine == "aurora_postgresql" else "INTEGER[]"
    fake = lambda *a, **k: TypeResolution(aurora_type=other, needs_judgment=False)  # noqa: E731
    monkeypatch.setattr(ddl_generator, "resolve_source_type", fake)
    with pytest.raises(ValueError, match=r"^column t\.c: aurora_type"):
        GENERATORS[engine]([_column(data_type="integer")], source_engine="postgresql")


def _functions(tree: ast.AST) -> dict[str, ast.FunctionDef]:
    return {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    return [
        c
        for c in ast.walk(node)
        if isinstance(c, ast.Call)
        and (getattr(c.func, "id", None) == name or getattr(c.func, "attr", None) == name)
    ]


def test_column_ddl_is_built_in_one_place_and_it_runs_the_allowlist():
    """A column definition is built only by ``_column_from``, which calls
    ``validate_aurora_type`` for the dialect, so no new path can write a column type
    around it. Scans every module under ``src/``."""
    builders = []
    for path in sorted((REPO / "src").rglob("*.py")):
        tree = ast.parse(path.read_text())
        for fn in _functions(tree).values():
            if _calls(fn, "ColumnDDL"):
                builders.append(f"{path.relative_to(REPO)}::{fn.name}")
    assert builders == ["src/tools/schema/aurora_common/ddl_generator.py::_column_from"]
    tree = ast.parse((REPO / "src/tools/schema/aurora_common/ddl_generator.py").read_text())
    (check,) = _calls(_functions(tree)["_column_from"], "validate_aurora_type")
    assert [ast.unparse(a) for a in check.args] == ["resolution.aurora_type", "dialect.name"]


# Every module under src/ with an f-string (or a %- / .format / + build) whose SQL starts
# with a DDL keyword, and the test that proves what it puts there. A new one fails
# test_every_ddl_writer_is_reviewed until it is added here with its proof.
DDL_WRITERS = {
    # The Aurora DDL generator: the property tests above.
    "src/tools/schema/aurora_common/ddl_generator.py",
    # step-1.ddl.sql around the generator's DDL: the step 1 tests below (schema, table and
    # database names are plain identifiers, checked when step 1 reads its inputs).
    "src/tools/data_movement/dms_step.py",
    # CREATE/DROP PUBLICATION on a live source: names pass an identifier allowlist first
    # (InitialLoadError), tests/unit/tools/data_movement/test_initial_load.py.
    "src/tools/data_movement/initial_load.py",
    # An in-memory DuckDB table of VARCHAR columns for the contract check, never Aurora
    # DDL: column names through quote_ident (quotes doubled).
    "src/tools/transformation_contract/duckdb_backend.py",
    # CREATE TYPE ... AS ENUM for step 1 (#1834): type names pass the identifier allowlist,
    # labels are string literals with quotes doubled; test_step1_enum_types_... below.
    "src/tools/transformation_contract/pg_enums.py",
    # The local graph store's own Cypher schema (table names from a fixed list), not SQL.
    "src/graph/store.py",
}
_DDL_START = re.compile(
    r"^\s*(?:-- )?(?:CREATE|ALTER|DROP|GRANT|TRUNCATE|USE|SET search_path)(?: |$)"
)  # a literal space after the keyword: a regex such as r"ALTER\s+INDEX" builds no DDL


def _starts_ddl(node: ast.AST) -> bool:
    if isinstance(node, ast.JoinedStr):
        first = node.values[0] if node.values else None
        return isinstance(first, ast.Constant) and bool(_DDL_START.match(str(first.value)))
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return _starts_ddl(node.left) or (
            isinstance(node.left, ast.Constant)
            and isinstance(node.left.value, str)
            and bool(_DDL_START.match(node.left.value))
        )
    if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "format":
        target = node.func.value  # type: ignore[attr-defined]
        return isinstance(target, ast.Constant) and bool(_DDL_START.match(str(target.value)))
    return False


@pytest.mark.passes_on_main
def test_every_ddl_writer_is_reviewed():
    """The modules that format DDL from values are exactly the reviewed list: a new one
    fails here until it is added with the test that proves its values are checked."""
    writers = {
        str(path.relative_to(REPO))
        for path in sorted((REPO / "src").rglob("*.py"))
        if any(_starts_ddl(n) for n in ast.walk(ast.parse(path.read_text())))
    }
    assert writers == DDL_WRITERS


@pytest.mark.passes_on_main
def test_the_writer_scan_finds_each_way_to_build_ddl():
    """The scan's own cases, so a writer cannot hide behind another string build."""
    for src in ('f"CREATE TABLE {t} ()"', '"ALTER TABLE " + t', '"DROP TABLE %s" % t',
                '"GRANT ALL ON {}".format(t)', "f'-- ALTER TABLE \"{t}\" X'", 'f"USE `{d}`"'):  # fmt: skip
        assert any(_starts_ddl(n) for n in ast.walk(ast.parse(src))), src
    for src in ('f"Created {n} tables"', '"CREATE TABLE t ()"', 'f"{kind} CREATE"'):
        assert not any(_starts_ddl(n) for n in ast.walk(ast.parse(src))), src


# --- step-1.ddl.sql (the data movement step) ---------------------------------------

_name = st.lists(st.sampled_from(_PIECES + ["-", "$", "_"]), max_size=4).map(
    lambda p: "".join(p).replace(".", "") + SENTINEL
)


def _step1_bundle(engine: str, schema: str, database: str, monkeypatch) -> DmsStepBundle:
    """The step 1 fixture, its source schema and database named by the caller."""
    monkeypatch.setattr(dms_step_fixtures, "_schema", lambda _engine: schema)
    bundle = make_bundle(engine)
    source = bundle.plan.source.model_copy(update={"database": database})
    return bundle.model_copy(update={"plan": bundle.plan.model_copy(update={"source": source})})


@settings(max_examples=scaled(200), deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])  # fmt: skip
@given(engine=st.sampled_from(sorted(GENERATORS)), schema=_name, database=_name)
def test_step1_ddl_takes_the_source_schema_and_database_only_as_plain_names(
    monkeypatch, engine, schema, database
):
    """``step-1.ddl.sql`` names the source schema (Aurora PostgreSQL) and database, in
    statements and in the commented ones the file tells the customer to uncomment. Step 1
    refuses a name that is not a plain identifier when it reads its inputs; a name it
    takes is never SQL code in the file, commented statements uncommented included."""
    bundle = _step1_bundle(engine, schema, database, monkeypatch)
    try:
        inputs = bundle_inputs(bundle)
    except DmsStepError as exc:
        assert "plain identifier" in str(exc) or "database name" in str(exc)
        return
    ddl = render_files(inputs)[DDL_FILE]
    assert SENTINEL not in sql_code(ddl, engine).lower()
    uncommented = re.sub(r"(?m)^-- (?=CREATE USER|GRANT|ALTER )", "", ddl)
    assert SENTINEL not in sql_code(uncommented, engine).lower()


@pytest.mark.parametrize("engine", sorted(GENERATORS))
@pytest.mark.parametrize(
    "database", ['shop"; DROP TABLE x; --', "shop`; DROP TABLE x; --", "shop\nDROP TABLE x;",
                 "shop\rDROP TABLE x;", "shop'", ""],
    ids=["double-quote", "backtick", "lf", "cr", "single-quote", "empty"],
)  # fmt: skip
def test_step1_refuses_a_source_database_that_is_not_a_plain_name(monkeypatch, engine, database):
    """The database name goes into ``CREATE DATABASE``/``USE`` (Aurora MySQL) and into the
    commented grants (both engines), where a line break would end the comment. Step 1
    checks it when it reads its inputs, with the pattern the DMS endpoint gives it, so
    ``render_ddl`` never sees one that is not a plain name."""
    with pytest.raises(DmsStepError, match="database name"):
        bundle_inputs(_step1_bundle(engine, "public", database, monkeypatch))


@pytest.mark.passes_on_main
def test_step1_takes_a_hyphenated_database_on_aurora_mysql(monkeypatch):
    """The negative case: Aurora MySQL's database name pattern takes a hyphen, and the
    file backtick-quotes it."""
    bundle = _step1_bundle("aurora_mysql", "public", "my-shop", monkeypatch)
    assert "CREATE DATABASE IF NOT EXISTS `my-shop`;" in render_ddl(bundle_inputs(bundle))


def test_step1_refuses_a_hyphenated_database_on_aurora_postgresql(monkeypatch):
    """Aurora PostgreSQL's database name pattern takes no hyphen; the file names the
    database in its commented grants, so step 1 refuses it when it reads its inputs."""
    bundle = _step1_bundle("aurora_postgresql", "public", "my-shop", monkeypatch)
    with pytest.raises(DmsStepError, match="database name"):
        bundle_inputs(bundle)


@settings(max_examples=scaled(200), deadline=None)
@given(name=st.one_of(_name, st.just("mood")), labels=st.lists(_hostile, max_size=4))
def test_step1_enum_types_take_names_only_as_identifiers_and_labels_only_as_literals(name, labels):
    """A PostgreSQL enum's name and labels are collector text (#1834). Step 1 keeps an enum
    only when its schema and name are plain identifiers; its labels reach ``step-1.ddl.sql``
    only inside string literals, the commented CREATE CAST uncommented included."""
    from src.contracts.collector_output import CollectorOutputContract
    from src.tools.transformation_contract.pg_enums import enum_columns
    from tests.unit.tools.transformation_contract.conftest import col, collector, table

    raw = collector("postgresql", [table("public", "t", [col("c", name, 1)], pk=["c"])])
    raw["database_schema"]["enum_types"] = [
        {"schema_name": "public", "type_name": name, "labels": labels}
    ]
    enums = list(enum_columns(CollectorOutputContract.model_validate(raw), "aurora_postgresql")
                 .types.values())  # fmt: skip
    for e in enums:
        assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", e.schema) and re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_$]*", e.name
        )
    inputs = dataclasses.replace(
        bundle_inputs(make_bundle("aurora_postgresql")), enum_types=tuple(enums)
    )
    ddl = render_ddl(inputs)
    assert SENTINEL not in sql_code(ddl, "aurora_postgresql").lower()
    uncommented = re.sub(r"(?m)^-- (?=CREATE CAST)", "", ddl)
    assert SENTINEL not in sql_code(uncommented, "aurora_postgresql").lower()
