# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's routine scripts (#1947): the deploy script the customer runs (the carried
routines in dependency order, PostgreSQL triggers created disabled, MySQL triggers and
events left out), the read-only check script, and where a routine's text may go."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.agents.collector.routine_sources import write_routine_sources
from src.contracts.transformation_contract import NOT_MOVED, Finding
from src.storage.local_store import LocalArtifactStore
from src.tools.data_movement.dms_step import render_ddl
from src.tools.data_movement.routine_scripts import (
    CHECK_FILE,
    DEPLOY_FILE,
    INSTRUCTIONS_FILE,
    RoutineScriptError,
    write_routine_scripts,
)
from src.tools.routines.migration import routine_migration_path
from tests.unit.tools.data_movement.dms_step_fixtures import make_contract, make_step1_inputs
from tests.unit.tools.data_movement.routine_fixtures import (
    SENTINEL,
    contract_with_routines,
    dep_routine,
    migration,
    mysql_source,
    pg_source,
    row,
)

SHA = "a" * 64
_PG_FN = "CREATE OR REPLACE FUNCTION public.{name}(p integer)\n RETURNS integer\n LANGUAGE sql\nAS $function$SELECT p$function$\n"


def _job(tmp_path: Path, artifact, sources, engine: str = "postgresql") -> LocalArtifactStore:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(routine_migration_path("shop", "job1", 2), artifact.model_dump(mode="json"))
    if sources is not None:
        collector = {"metadata": {"source_database": {"engine": engine, "version": "16.2"}}}
        write_routine_sources(store, "shop", "job1", collector, sources)
    return store


def _write(tmp_path, store, contract, **kw):
    return write_routine_scripts(store, "shop", "job1", contract, SHA, tmp_path / "step", **kw)


def _pg_case(tmp_path, sources=None):
    rows = [
        row("public.audit()", specific="audit_1"),
        row("public.orders.trg", "trigger", name="trg", table="public.orders", specific="orders.trg",
            dependencies=[dep_routine("function", "public.audit()")]),
        row("public.later(integer)", specific="later_2",
            dependencies=[dep_routine("function", "public.audit()")]),
        row("public.py_fn()", specific="py_3", status="needs_attention",
            reasons=["postgresql-routine-language"]),
    ]  # fmt: skip
    artifact = migration(rows)
    if sources is None:
        sources = [
            pg_source("audit_1", _PG_FN.format(name="audit").replace("SELECT p", f"SELECT '{SENTINEL}'")),
            pg_source("orders.trg", "CREATE TRIGGER trg BEFORE INSERT ON public.orders FOR EACH ROW "
                      "EXECUTE FUNCTION public.audit()", kind="trigger"),
            pg_source("later_2", _PG_FN.format(name="later")),
            pg_source("py_3", "CREATE FUNCTION public.py_fn() RETURNS int LANGUAGE plpythonu AS $$ return 1 $$"),
        ]  # fmt: skip
    return _job(tmp_path, artifact, sources), contract_with_routines(artifact)


def test_the_postgresql_deploy_script_carries_in_order_and_disables_each_trigger_in_its_transaction(
    tmp_path,
):
    store, contract = _pg_case(tmp_path)
    scripts = _write(tmp_path, store, contract)
    assert (scripts.status, scripts.carried, scripts.in_script, scripts.refused) == (
        "ready",
        3,
        3,
        [],
    )
    text = (tmp_path / "step" / DEPLOY_FILE).read_text()
    begin, commit = text.index("\nBEGIN;\n"), text.rindex("COMMIT;")
    audit = text.index("FUNCTION public.audit(")
    trigger = text.index("CREATE TRIGGER trg")
    disable = text.index('ALTER TABLE "public"."orders" DISABLE TRIGGER "trg";')
    later = text.index("FUNCTION public.later(")
    assert begin < audit < trigger < disable < later < commit
    assert "SET check_function_bodies = on;" in text[:begin] and "ON_ERROR_STOP" in text[:begin]
    assert "plpythonu" not in text  # not carried, so never written
    assert "Checked, not load-tested" in text


def test_a_routine_text_and_its_literals_reach_only_the_deploy_script(tmp_path, capsys):
    store, contract = _pg_case(tmp_path)
    _write(tmp_path, store, contract)
    holding = sorted(
        str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")
        if p.is_file() and SENTINEL.encode() in p.read_bytes()
    )  # fmt: skip
    assert holding == [f"step/{DEPLOY_FILE}"]
    assert SENTINEL not in capsys.readouterr().out


def test_without_routine_sources_every_carried_routine_is_left_out_by_name(tmp_path):
    rows = [row("public.audit()", specific="audit_1")]
    artifact = migration(rows)
    store = _job(tmp_path, artifact, None)
    scripts = _write(tmp_path, store, contract_with_routines(artifact))
    assert scripts.refused == [("function", "public.audit()", "source_missing")]
    assert scripts.in_script == 0
    assert "CREATE" not in (tmp_path / "step" / DEPLOY_FILE).read_text()


def test_a_routine_naming_one_left_out_is_left_out_too(tmp_path):
    store, contract = _pg_case(tmp_path, sources=[
        pg_source("orders.trg", "CREATE TRIGGER trg BEFORE INSERT ON public.orders FOR EACH ROW "
                  "EXECUTE FUNCTION public.audit()", kind="trigger"),
        pg_source("later_2", _PG_FN.format(name="later")),
    ])  # fmt: skip
    refused = {i: c for _, i, c in _write(tmp_path, store, contract).refused}
    assert refused == {"public.audit()": "source_missing", "public.orders.trg": "depends_on_refused",
                       "public.later(integer)": "depends_on_refused"}  # fmt: skip


def test_a_name_that_could_break_out_of_a_script_line_is_refused(tmp_path):
    rows = [row('public."bad\nname"()', name="bad\nname", specific="bad_1")]
    artifact = migration(rows)
    store = _job(tmp_path, artifact, [pg_source("bad_1", _PG_FN.format(name='"bad\nname"'))])
    scripts = _write(tmp_path, store, contract_with_routines(artifact))
    assert scripts.refused == [("function", 'public."bad\nname"()', "unsafe_name")]
    assert "bad\nname" not in (tmp_path / "step" / CHECK_FILE).read_text()


def _mysql_case(tmp_path, **source_kw):
    rows = [
        row("shop.place_order", "procedure", schema="shop", status="rewritten",
            signature="IN int", specific="place_order"),
        row("shop.orders_bi", "trigger", schema="shop", table="shop.orders", specific="orders.orders_bi"),
        row("shop.nightly", "event", schema="shop", specific="nightly"),
    ]  # fmt: skip
    artifact = migration(rows, "mysql")
    sources = [
        mysql_source("place_order", "CREATE DEFINER=`app`@`%` PROCEDURE `place_order`(IN p_id INT)\n"
                     f"BEGIN\n  SELECT '{SENTINEL}', p_id;\nEND", **source_kw),
        mysql_source("orders.orders_bi", "CREATE DEFINER=`app`@`%` TRIGGER `orders_bi` BEFORE INSERT "
                     "ON `orders` FOR EACH ROW SET NEW.id = NEW.id", kind="trigger"),
        mysql_source("nightly", "CREATE DEFINER=`app`@`%` EVENT `nightly` ON SCHEDULE EVERY 1 DAY "
                     "DO DELETE FROM orders WHERE id < 0", kind="event"),
    ]  # fmt: skip
    return _job(tmp_path, artifact, sources, "mysql"), contract_with_routines(artifact)


def test_the_mysql_deploy_script_rewrites_the_definer_and_leaves_triggers_and_events_out(tmp_path):
    store, contract = _mysql_case(tmp_path)
    scripts = _write(tmp_path, store, contract, target_database="shop_new")
    assert (scripts.in_script, scripts.deferred, scripts.refused) == (1, 2, [])
    text = (tmp_path / "step" / DEPLOY_FILE).read_text()
    assert "CREATE DEFINER=`admin`@`%` PROCEDURE `place_order`" in text
    assert "`app`" not in text and "TRIGGER" not in text.split("SET NAMES")[1]
    assert "EVENT `nightly`" not in text
    assert "USE `shop_new`;" in text and "SET SESSION sql_mode = 'STRICT_TRANS_TABLES';" in text
    assert "DELIMITER ;;" in text and text.count("DELIMITER ;\n") == 1
    check = (tmp_path / "step" / CHECK_FILE).read_text()
    assert "information_schema.TRIGGERS" in check and "information_schema.EVENTS" in check
    assert "('shop_new', 'orders_bi')" in check and "START TRANSACTION READ ONLY;" in check
    assert "admin" in (tmp_path / "step" / INSTRUCTIONS_FILE).read_text()


def test_the_target_user_names_the_definer(tmp_path):
    store, contract = _mysql_case(tmp_path)
    _write(tmp_path, store, contract, target_user="migrator")
    assert "DEFINER=`migrator`@`%`" in (tmp_path / "step" / DEPLOY_FILE).read_text()


@pytest.mark.parametrize("session", [{"sql_mode": "STRICT'; DROP TABLE x; --"},
                                     {"collation": "utf8mb4' OR '1"}])  # fmt: skip
def test_a_session_value_outside_the_allowlist_is_refused_never_written(tmp_path, session):
    store, contract = _mysql_case(tmp_path, **session)
    scripts = _write(tmp_path, store, contract)
    assert scripts.refused == [("procedure", "shop.place_order", "session_not_allowed")]
    assert "DROP TABLE x" not in (tmp_path / "step" / DEPLOY_FILE).read_text()
    assert " OR '1" not in (tmp_path / "step" / DEPLOY_FILE).read_text()


def test_nothing_to_deploy_writes_no_file(tmp_path):
    artifact = migration([row("public.a()", status="needs_attention", reasons=["x"])])
    store = _job(tmp_path, artifact, [])
    assert _write(tmp_path, store, contract_with_routines(artifact)).status == "nothing_to_deploy"
    assert not (tmp_path / "step").exists()


def test_a_contract_written_before_the_section_is_an_error(tmp_path):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    with pytest.raises(RoutineScriptError, match="no routines section"):
        _write(tmp_path, store, make_contract("aurora_postgresql"))


def test_the_postgresql_deploy_fails_fast_on_a_lock_instead_of_dms(tmp_path):
    store, contract = _pg_case(tmp_path)
    _write(tmp_path, store, contract)
    text = (tmp_path / "step" / DEPLOY_FILE).read_text()
    begin = text.index("\nBEGIN;\n")
    assert text[begin:].startswith("\nBEGIN;\nSET LOCAL lock_timeout = '500ms';\n")
    assert text.index("SET LOCAL lock_timeout") < text.index("CREATE OR REPLACE FUNCTION")
    assert "lock timeout" in (tmp_path / "step" / INSTRUCTIONS_FILE).read_text()


def test_a_mysql_body_with_a_delimiter_line_is_refused(tmp_path):
    store, contract = _mysql_case(tmp_path)
    sources = [mysql_source("place_order", "CREATE DEFINER=`app`@`%` PROCEDURE `place_order`(IN p INT)\n"
                            "BEGIN\n  SELECT 'a\ndelimiter //\nb', p;\nEND")]  # fmt: skip
    write_routine_sources(store, "shop", "job1", {"metadata": {"source_database": {"engine": "mysql"}}},
                          sources)  # fmt: skip
    refused = {i: c for _, i, c in _write(tmp_path, store, contract).refused}
    assert refused == {"shop.place_order": "delimiter_in_text"}
    assert "delimiter //" not in (tmp_path / "step" / DEPLOY_FILE).read_text()


def test_a_mysql_routine_text_reaches_only_the_deploy_script(tmp_path, capsys):
    store, contract = _mysql_case(tmp_path)
    _write(tmp_path, store, contract)
    holding = sorted(
        str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")
        if p.is_file() and SENTINEL.encode() in p.read_bytes()
    )  # fmt: skip
    assert holding == [f"step/{DEPLOY_FILE}"]
    assert SENTINEL not in capsys.readouterr().out


def test_nothing_to_deploy_removes_an_earlier_runs_scripts(tmp_path):
    store, contract = _pg_case(tmp_path)
    _write(tmp_path, store, contract)
    artifact = migration([row("public.a()", status="needs_attention", reasons=["x"])])
    store.write_json(routine_migration_path("shop", "job1", 2), artifact.model_dump(mode="json"))
    assert _write(tmp_path, store, contract_with_routines(artifact)).status == "nothing_to_deploy"
    assert sorted(p.name for p in (tmp_path / "step").iterdir()) == []


def test_a_postgresql_routine_with_a_text_rewrite_is_refused_not_deployed_without_it(tmp_path):
    store, _ = _pg_case(tmp_path)
    pinned = row("public.audit()", specific="audit_1", status="rewritten")
    pinned["rewrites"] = [
        {"rule_id": "postgresql-routine-pin-search-path", "rewrite": "search_path"}
    ]
    artifact = migration([pinned])
    store.write_json(routine_migration_path("shop", "job1", 2), artifact.model_dump(mode="json"))
    scripts = _write(tmp_path, store, contract_with_routines(artifact))
    assert scripts.refused == [("function", "public.audit()", "rewrite_not_reapplied")]


# --------------------------------------------------------------------------- schemas (#2148)

_SCHEMA_FN = "CREATE OR REPLACE FUNCTION {schema}.{name}(p integer)\n RETURNS integer\n LANGUAGE sql\nAS $function$SELECT p$function$\n"


def _pg_schemas_case(tmp_path, schemas: list[str], contract=None):
    """One function per schema in ``schemas``, in that deploy order."""
    rows, sources = [], []
    for i, schema in enumerate(schemas):
        name = f"fn{i}"
        rows.append(row(f"{schema}.{name}(integer)", schema=schema, name=name, specific=f"{name}_{i}",
                        signature="integer"))  # fmt: skip
        quoted = '"' + schema.replace('"', '""') + '"'
        sources.append(pg_source(f"{name}_{i}", _SCHEMA_FN.format(schema=quoted, name=name),
                                 schema=schema))  # fmt: skip
    artifact = migration(rows)
    store = _job(tmp_path, artifact, sources)
    scripts = _write(tmp_path, store, contract_with_routines(artifact, contract=contract))
    assert scripts.in_script == len(schemas) and scripts.refused == []
    return (tmp_path / "step" / DEPLOY_FILE).read_text()


def _schema_lines(text: str, verb: str = "CREATE SCHEMA") -> list[str]:
    return [line for line in text.splitlines() if line.startswith(verb)]


def test_the_postgresql_deploy_creates_the_routines_schema_in_its_transaction_first(tmp_path):
    text = _pg_schemas_case(tmp_path, ["discourse_functions"])
    assert _schema_lines(text) == ['CREATE SCHEMA IF NOT EXISTS "discourse_functions";']
    begin = text.index("\nBEGIN;\n")
    lock = text.index("SET LOCAL lock_timeout")
    schema = text.index('CREATE SCHEMA IF NOT EXISTS "discourse_functions";')
    path = text.index('SET search_path TO "discourse_functions", public;')
    create = text.index("CREATE OR REPLACE FUNCTION")
    assert begin < lock < schema < path < create < text.rindex("COMMIT;")


def _contract_in(
    engine: str, moved: str, *, blocked: str | None = None, not_moved: str | None = None
):
    """make_contract's three tables with orders moved in ``moved``, customers blocked in
    ``blocked`` and audit_log not moved (#1918) in ``not_moved``; each else moved in ``moved``."""
    rules = []
    for r in make_contract(engine).rules:
        name = r.target_table
        schema = {"customers": blocked, "audit_log": not_moved}.get(name) or moved
        table_id = f"{schema}.{name}"
        update: dict = {"source_table": table_id}
        if name == "customers" and blocked:
            update |= {
                "status": "blocked",
                "findings": [
                    Finding(
                        code="unknown_table", severity="error", message="blocked", table_id=table_id
                    )
                ],
            }
        if name == "audit_log" and not_moved:
            update |= {
                "moves_rows": False,
                "findings": [
                    Finding(
                        code=NOT_MOVED,
                        severity="warning",
                        message="not moved",
                        table_id=table_id,
                        column="msg",
                    )
                ],
            }
        rules.append(r.model_copy(update=update))
    return make_contract(engine).model_copy(update={"rules": rules})


@pytest.mark.passes_on_main
def test_public_is_never_created_even_when_the_moved_tables_are_elsewhere(tmp_path):
    # The moved tables are in shop, so only the public skip keeps public out.
    contract = _contract_in("aurora_postgresql", "shop")
    assert _schema_lines(_pg_schemas_case(tmp_path, ["public", "shop"], contract)) == []


def test_a_schema_holding_only_a_not_moved_table_is_created(tmp_path):
    # step-1.ddl.sql creates only the moved tables' schema (#1918): reporting is not it.
    contract = _contract_in("aurora_postgresql", "public", not_moved="reporting")
    text = _pg_schemas_case(tmp_path, ["reporting"], contract)
    assert _schema_lines(text) == ['CREATE SCHEMA IF NOT EXISTS "reporting";']


def test_a_schema_holding_only_a_blocked_table_is_created(tmp_path):
    contract = _contract_in("aurora_postgresql", "public", blocked="reporting")
    text = _pg_schemas_case(tmp_path, ["reporting"], contract)
    assert _schema_lines(text) == ['CREATE SCHEMA IF NOT EXISTS "reporting";']


def test_each_routine_schema_is_created_once_and_public_and_the_tables_schema_never(tmp_path):
    # make_contract's PostgreSQL tables are in public, which the step 1 DDL creates.
    text = _pg_schemas_case(tmp_path, ["reporting", "public", "discourse_functions", "reporting"])
    assert _schema_lines(text) == [
        'CREATE SCHEMA IF NOT EXISTS "discourse_functions";',
        'CREATE SCHEMA IF NOT EXISTS "reporting";',
    ]
    assert text.index('"reporting";') < text.index("CREATE OR REPLACE FUNCTION")


def test_a_schema_name_that_needs_quotes_is_quoted(tmp_path):
    text = _pg_schemas_case(tmp_path, ['Odd "Schema"'])
    assert _schema_lines(text) == ['CREATE SCHEMA IF NOT EXISTS "Odd ""Schema""";']


@pytest.mark.passes_on_main
def test_a_schema_only_a_refused_routine_uses_is_not_created(tmp_path):
    rows = [row("discourse_functions.f(integer)", schema="discourse_functions", name="f",
                specific="f_1", signature="integer")]  # fmt: skip
    artifact = migration(rows)
    store = _job(tmp_path, artifact, [])  # its source is missing, so it is left out
    scripts = _write(tmp_path, store, contract_with_routines(artifact))
    assert scripts.refused == [("function", "discourse_functions.f(integer)", "source_missing")]
    assert _schema_lines((tmp_path / "step" / DEPLOY_FILE).read_text()) == []


def _mysql_schemas_case(tmp_path, schemas: list[str], collations=None, **kw):
    rows, sources = [], []
    for i, schema in enumerate(schemas):
        name = f"p{i}"
        rows.append(row(f"{schema}.{name}", "procedure", schema=schema, name=name, specific=name,
                        status="rewritten", signature="IN int"))  # fmt: skip
        sources.append(mysql_source(name, f"CREATE DEFINER=`app`@`%` PROCEDURE `{name}`(IN p INT)\n"
                                    "BEGIN\n  SELECT p;\nEND", schema=schema,
                                    database_collation=(collations or {}).get(i)))  # fmt: skip
    artifact = migration(rows, "mysql")
    store = _job(tmp_path, artifact, sources, "mysql")
    scripts = _write(tmp_path, store, contract_with_routines(artifact), **kw)
    assert scripts.in_script == len(schemas) and scripts.refused == []
    return (tmp_path / "step" / DEPLOY_FILE).read_text()


def test_the_mysql_deploy_creates_each_routine_database_the_tables_are_not_in(tmp_path):
    # make_contract's MySQL tables are in shop, which the step 1 DDL creates.
    text = _mysql_schemas_case(tmp_path, ["shop", "shop_functions", "odd`db"])
    assert _schema_lines(text, "CREATE DATABASE") == [
        "CREATE DATABASE IF NOT EXISTS `odd``db`;",
        "CREATE DATABASE IF NOT EXISTS `shop_functions`;",
    ]
    names = text.index("SET NAMES utf8mb4;")
    created = text.index("CREATE DATABASE IF NOT EXISTS `shop_functions`;")
    assert names < created < text.index("DELIMITER") < text.index("USE `shop_functions`;")


def test_a_mysql_target_database_the_tables_are_not_in_is_created(tmp_path):
    text = _mysql_schemas_case(tmp_path, ["shop"], target_database="shop_new")
    assert _schema_lines(text, "CREATE DATABASE") == ["CREATE DATABASE IF NOT EXISTS `shop_new`;"]
    assert _schema_lines(_mysql_schemas_case(tmp_path, ["shop"], target_database="shop"),
                         "CREATE DATABASE") == []  # fmt: skip


def _flat_comments(text: str) -> str:
    """A script's ``--`` comment text as one line, so a wrapped sentence reads whole."""
    return " ".join(line[3:].strip() for line in text.splitlines() if line.startswith("-- "))


def _database_clauses(text: str) -> set[str]:
    """What follows the name in each ``CREATE DATABASE IF NOT EXISTS`` (its options)."""
    return set(re.findall(r"^CREATE DATABASE IF NOT EXISTS `(?:[^`]|``)+`(.*);$", text, re.M))


def test_a_routine_database_is_created_as_the_step_ddl_creates_the_tables_database(tmp_path):
    # step-1.ddl.sql creates the tables' database with no character set or collation, so the
    # tables take the server default; a routine database must take the same, or a routine
    # comparing a parameter with a moved column fails with an illegal mix of collations.
    text = _mysql_schemas_case(tmp_path, ["shop_functions"],
                               collations={0: "utf8mb4_unicode_ci"})  # fmt: skip
    ddl = render_ddl(make_step1_inputs("aurora_mysql"))
    assert _database_clauses(text) == _database_clauses(ddl) == {""}
    assert "COLLATE" not in text and "CHARACTER SET" not in text
    found = re.search(r"It sets no character set or collation: .*? before you run it\.",
                      _flat_comments(ddl))  # fmt: skip
    assert found and found.group(0) in _flat_comments(text)


@pytest.mark.parametrize("engine", ["postgresql", "mysql"])
def test_the_scripts_say_the_new_schemas_grants_are_the_customers(tmp_path, engine):
    if engine == "postgresql":
        text = _pg_schemas_case(tmp_path / "a", ["discourse_functions"])
        grant, kind = "USAGE on each schema", "Schemas"
    else:
        text = _mysql_schemas_case(tmp_path / "a", ["shop_functions"])
        grant, kind = "EXECUTE on each database", "Databases"
    sentence = f"the script grants nothing: {grant} for the application user is yours to add."
    assert sentence in text
    md = (tmp_path / "a" / "step" / INSTRUCTIONS_FILE).read_text()
    assert f"- {kind} it creates, which step 1's DDL does not: 1" in md
    assert f"the script grants nothing: `{grant.split()[0]}`" in md
    if engine == "postgresql":
        _pg_schemas_case(tmp_path / "b", ["public"])
    else:
        _mysql_schemas_case(tmp_path / "b", ["shop"])
    assert "grants nothing" not in (tmp_path / "b" / "step" / INSTRUCTIONS_FILE).read_text()


def test_a_mysql_routine_database_that_differs_from_the_tables_only_by_case_is_refused(tmp_path):
    # With lower_case_table_names=0, `Shop` would be a second, empty database: refused instead.
    rows = [
        row("Shop.p0", "procedure", schema="Shop", name="p0", specific="p0", status="rewritten",
            signature="IN int"),
        row("shop.p1", "procedure", schema="shop", name="p1", specific="p1", status="rewritten",
            signature="IN int", dependencies=[dep_routine("procedure", "Shop.p0")]),
    ]  # fmt: skip
    sources = [mysql_source(n, f"CREATE DEFINER=`app`@`%` PROCEDURE `{n}`(IN p INT)\n"
                            "BEGIN\n  SELECT p;\nEND", schema=d) for n, d in (("p0", "Shop"), ("p1", "shop"))]  # fmt: skip
    artifact = migration(rows, "mysql")
    store = _job(tmp_path, artifact, sources, "mysql")
    scripts = _write(tmp_path, store, contract_with_routines(artifact))
    assert scripts.refused == [("procedure", "Shop.p0", "database_case_differs"),
                               ("procedure", "shop.p1", "depends_on_refused")]  # fmt: skip
    text = (tmp_path / "step" / DEPLOY_FILE).read_text()
    assert "`Shop`" not in text and _schema_lines(text, "CREATE DATABASE") == []
    md = " ".join((tmp_path / "step" / INSTRUCTIONS_FILE).read_text().split())
    assert ("Left out because their database differs from a moved table's database only by "
            "case: 1 (`database_case_differs`)") in md  # fmt: skip
    assert "cannot tell whether the target folds case" in md
    assert "Rename one side on the source, or create these routines by hand" in md
