# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The routine cutover scripts (#1948): the script the customer runs at their own cutover
(PostgreSQL triggers enabled; the MySQL triggers and events step 1 deferred, created),
its read-only check script, the instructions, and where a routine's text may go."""

from __future__ import annotations

import json

import pytest

from src.tools.data_movement.cutover_scripts import (
    CUTOVER_CHECK_FILE,
    CUTOVER_FILE,
    CUTOVER_INSTRUCTIONS,
    event_state,
    write_cutover_scripts,
)
from src.tools.data_movement.routine_scripts import RoutineScriptError
from tests.unit.tools.data_movement.dms_step_fixtures import make_contract
from tests.unit.tools.data_movement.routine_fixtures import (
    SENTINEL,
    contract_with_routines,
    dep_routine,
    migration,
    mysql_source,
    pg_source,
    row,
)
from tests.unit.tools.data_movement.test_routine_scripts import _PG_FN, SHA, _job
from tests.unit.tools.sql._timing import assert_cost_ratio

_TRIGGER = (
    "CREATE TRIGGER {name} BEFORE INSERT ON public.orders FOR EACH ROW EXECUTE FUNCTION "
    "public.audit()"
)


def _cut(tmp_path, store, contract, **kw):
    return write_cutover_scripts(store, "shop", "job1", contract, SHA, tmp_path / "step", **kw)


def _read(tmp_path, name):
    return (tmp_path / "step" / name).read_text()


def _states(artifact, states=None, default="O"):
    """What the collection gives each trigger of ``artifact``: its source state."""
    return {r.routine_id: {"source_state": (states or {}).get(r.routine_id, default)}
            for r in artifact.routines if r.kind == "trigger"}  # fmt: skip


def _pg_case(tmp_path, triggers=("trg_a", "trg_b"), extra_rows=(), extra_sources=(), states=None):
    rows = [row("public.audit()", specific="audit_1", signature="")]
    sources = [
        pg_source(
            "audit_1", _PG_FN.format(name="audit").replace("SELECT p", f"SELECT '{SENTINEL}'")
        )
    ]
    for name in triggers:
        rows.append(row(f"public.orders.{name}", "trigger", name=name, table="public.orders",
                        specific=f"orders.{name}",
                        dependencies=[dep_routine("function", "public.audit()")]))  # fmt: skip
        sources.append(pg_source(f"orders.{name}", _TRIGGER.format(name=name), kind="trigger"))
    artifact = migration([*rows, *extra_rows])
    contract = contract_with_routines(artifact, triggers=_states(artifact, states))
    return _job(tmp_path, artifact, [*sources, *extra_sources]), contract


def test_the_postgresql_cutover_enables_each_carried_trigger_in_one_transaction(tmp_path):
    store, contract = _pg_case(tmp_path)
    scripts = _cut(tmp_path, store, contract)
    assert (scripts.status, scripts.expected, scripts.in_script, scripts.excluded) == (
        "ready", 2, 2, [])  # fmt: skip
    text = _read(tmp_path, CUTOVER_FILE)
    body = text.split("BEGIN;", 1)[1]
    assert body.index('ENABLE TRIGGER "trg_a"') < body.index('ENABLE TRIGGER "trg_b"')
    assert 'ALTER TABLE "public"."orders" ENABLE TRIGGER "trg_a";' in body
    assert "\\set ON_ERROR_STOP on" in text and body.rstrip().endswith("COMMIT;")
    assert "SET client_encoding = 'UTF8';" in text.split("BEGIN;", 1)[0]
    # No rollback section, and no routine text: the triggers already exist (step 1 made them).
    assert "DISABLE TRIGGER" not in text and "CREATE" not in body and SENTINEL not in text
    check = _read(tmp_path, CUTOVER_CHECK_FILE)
    assert "modernizer-cutover-check" in check and "BEGIN TRANSACTION READ ONLY;" in check
    assert "('public', 'orders', 'trg_a')" in check and "pg_proc" not in check


def test_the_instructions_say_what_the_triggers_do_and_that_there_is_no_rollback(tmp_path):
    """What the files do and how to run them; never when (#2062)."""
    store, contract = _pg_case(tmp_path)
    _cut(tmp_path, store, contract)
    md = _read(tmp_path, CUTOVER_INSTRUCTIONS)
    assert md.count("The cutover is your responsibility.") == 1
    assert "session_replication_role = replica" in md and "ALWAYS (A) or REPLICA (R)" in md
    assert "No rollback" in md and "recreating them on Aurora is yours" in md
    assert md.startswith(
        "The cutover is your responsibility. The modernizer does not run, schedule or advise on it.\n"
    )
    assert "The modernizer never connects to your databases" in md
    assert "cutover-check.tsv" in md and "run_dms_step.py cutover-check" in md
    for advice in ("stopped writing", "applied the last change", "the gate passes again",
                   "point your applications at Aurora", "must not overlap"):  # fmt: skip
        assert advice not in md, advice
    sql = _read(tmp_path, CUTOVER_FILE)
    assert "The cutover is your responsibility." in sql and "applied the last change" not in sql


def test_no_trigger_means_nothing_to_do_and_removes_an_earlier_runs_files(tmp_path):
    store, contract = _pg_case(tmp_path)
    _cut(tmp_path, store, contract)
    store, contract = _pg_case(tmp_path, triggers=())
    scripts = _cut(tmp_path, store, contract)
    assert (scripts.status, scripts.expected, scripts.files) == ("nothing_to_do", 0, {})
    assert not any((tmp_path / "step" / n).exists() for n in
                   (CUTOVER_FILE, CUTOVER_CHECK_FILE, CUTOVER_INSTRUCTIONS))  # fmt: skip


def test_a_trigger_step_1_did_not_carry_or_did_not_deploy_is_left_out_and_named(tmp_path):
    rows = [
        row("public.py_fn()", specific="py_2", status="needs_attention",
            reasons=["postgresql-routine-language"]),
        row("public.orders.trg_py", "trigger", name="trg_py", table="public.orders",
            specific="orders.trg_py", dependencies=[dep_routine("function", "public.py_fn()")]),
        row("public.orders.trg_gone", "trigger", name="trg_gone", table="public.orders",
            specific="orders.trg_gone", dependencies=[dep_routine("function", "public.audit()")]),
    ]  # fmt: skip
    # trg_gone's source is missing: step 1's deploy script left it out, so it never exists.
    store, contract = _pg_case(tmp_path, triggers=("trg_a",), extra_rows=rows, extra_sources=[
        pg_source("py_2", "CREATE FUNCTION public.py_fn() RETURNS trigger LANGUAGE plpythonu AS $$ x $$")])  # fmt: skip
    scripts = _cut(tmp_path, store, contract)
    assert (scripts.expected, scripts.in_script) == (2, 1)
    assert scripts.excluded == [
        ("trigger", "public.orders.trg_gone", "source_missing"),
        ("trigger", "public.orders.trg_py", "depends_on_not_carried"),
    ]
    text = _read(tmp_path, CUTOVER_FILE)
    assert "-- Left out: trigger public.orders.trg_gone (source_missing)" in text
    assert '"trg_gone"' not in text and '"trg_py"' not in text and '"trg_a"' in text


def test_a_trigger_whose_function_step_1_left_out_is_left_out_too(tmp_path):
    rows = [row("public.audit()", specific="audit_1", signature="")]
    rows.append(row("public.orders.trg", "trigger", name="trg", table="public.orders",
                    specific="orders.trg", dependencies=[dep_routine("function", "public.audit()")]))  # fmt: skip
    artifact = migration(rows)
    # audit()'s text is not exact: the deploy refuses it, and the trigger it names with it.
    inexact = pg_source("audit_1", _PG_FN.format(name="audit")).model_copy(
        update={"exact": False, "inexact_reason": "x"})  # fmt: skip
    store = _job(tmp_path, artifact, [inexact, pg_source("orders.trg", _TRIGGER.format(name="trg"),
                                                         kind="trigger")])  # fmt: skip
    scripts = _cut(tmp_path, store, contract_with_routines(artifact, triggers=_states(artifact)))
    assert scripts.excluded == [("trigger", "public.orders.trg", "depends_on_refused")]
    assert "ENABLE TRIGGER" not in _read(tmp_path, CUTOVER_FILE)


def test_hostile_identifiers_are_quoted_or_refused(tmp_path):
    rows = [
        row('public.orders.tr"g', "trigger", name='tr"g', table="public.orders",
            specific='orders.tr"g', dependencies=[dep_routine("function", "public.audit()")]),
        row("public.orders.bad", "trigger", name="bad\n; DROP TABLE x; --", table="public.orders",
            specific="orders.bad", dependencies=[dep_routine("function", "public.audit()")]),
    ]  # fmt: skip
    sources = [pg_source('orders.tr"g', 'CREATE TRIGGER "tr""g" BEFORE INSERT ON public.orders '
                         "FOR EACH ROW EXECUTE FUNCTION public.audit()", kind="trigger"),
               pg_source("orders.bad", _TRIGGER.format(name="bad"), kind="trigger")]  # fmt: skip
    store, contract = _pg_case(tmp_path, triggers=(), extra_rows=rows, extra_sources=sources)
    scripts = _cut(tmp_path, store, contract)
    assert scripts.excluded == [("trigger", "public.orders.bad", "unsafe_name")]
    text = _read(tmp_path, CUTOVER_FILE)
    assert 'ALTER TABLE "public"."orders" ENABLE TRIGGER "tr""g";' in text
    assert "DROP TABLE" not in text and "DROP TABLE" not in _read(tmp_path, CUTOVER_CHECK_FILE)


def test_each_postgresql_trigger_is_set_to_exactly_its_source_state(tmp_path):
    names = ("t_o", "t_a", "t_r", "t_d")
    states = {f"public.orders.{n}": n[-1].upper() for n in names}
    store, contract = _pg_case(tmp_path, triggers=names, states=states)
    scripts = _cut(tmp_path, store, contract)
    assert (scripts.expected, scripts.in_script, scripts.stays_disabled) == (4, 4, 1)
    body = _read(tmp_path, CUTOVER_FILE).split("BEGIN;", 1)[1]
    assert 'ALTER TABLE "public"."orders" ENABLE TRIGGER "t_o";' in body
    assert 'ALTER TABLE "public"."orders" ENABLE ALWAYS TRIGGER "t_a";' in body
    assert 'ALTER TABLE "public"."orders" ENABLE REPLICA TRIGGER "t_r";' in body
    assert (
        '"t_d"' not in body
        and "trigger public.orders.t_d: stays disabled, as on the source" in body
    )
    assert "as the collection gives it: 1 stay disabled" in _read(tmp_path, CUTOVER_INSTRUCTIONS)


def test_a_state_the_collection_gives_only_as_enabled_or_disabled_claims_no_more(tmp_path):
    from src.tools.data_movement.cutover_scripts import SOURCE_ALWAYS_REPLICA_QUERY

    states = {"public.orders.trg_a": "enabled", "public.orders.trg_b": "disabled"}
    store, contract = _pg_case(tmp_path, states=states)
    scripts = _cut(tmp_path, store, contract)
    assert (scripts.in_script, scripts.stays_disabled, scripts.state_folded) == (2, 1, 2)
    text = _read(tmp_path, CUTOVER_FILE)
    body = text.split("BEGIN;", 1)[1]
    assert "trigger public.orders.trg_a (enabled on the source)" in body
    assert 'ALTER TABLE "public"."orders" ENABLE TRIGGER "trg_a";' in body
    assert "trigger public.orders.trg_b: stays disabled, disabled on the source" in body
    assert "source state" not in body and "as on the source" not in body
    note = "the collection records only enabled or disabled"
    assert note in text.split("BEGIN;", 1)[0]
    md = _read(tmp_path, CUTOVER_INSTRUCTIONS)
    assert note in md and "check those by hand" in md and SOURCE_ALWAYS_REPLICA_QUERY in md
    assert "tgenabled IN ('A', 'R')" in SOURCE_ALWAYS_REPLICA_QUERY
    assert "ENABLE ALWAYS" not in body and "ENABLE REPLICA" not in body


def test_exact_letters_need_no_hand_check(tmp_path):
    store, contract = _pg_case(tmp_path)
    _cut(tmp_path, store, contract)
    assert "check those by hand" not in _read(tmp_path, CUTOVER_INSTRUCTIONS)
    assert "(source state O)" in _read(tmp_path, CUTOVER_FILE)


@pytest.mark.parametrize(
    "letters, says",
    [
        (("R", "D"), "sets the 2 triggers step 1 created disabled to their source state; "
                     "1 of them stay disabled"),
        (("enabled", "disabled"), "sets the 2 triggers step 1 created disabled enabled or "
                                  "disabled as the collection records; 1 of them stay disabled"),
        (("A", "disabled"), "sets the 2 triggers step 1 created disabled: 1 to their source "
                            "state, 1 enabled or disabled as the collection records; 1 of them "
                            "stay disabled"),
    ],
)  # fmt: skip
def test_the_header_claims_the_source_state_only_for_exact_letters(tmp_path, letters, says):
    names = ("t_x", "t_y")
    states = {f"public.orders.{n}": s for n, s in zip(names, letters, strict=True)}
    store, contract = _pg_case(tmp_path, triggers=names, states=states)
    _cut(tmp_path, store, contract)
    head = _read(tmp_path, CUTOVER_FILE).split("BEGIN;", 1)[0]
    assert f"-- This script {says}; 2 expected" in head
    if "enabled" in letters or "disabled" in letters:
        assert "to their source state;" not in head


def test_a_postgresql_trigger_whose_source_state_is_unknown_is_left_out_never_guessed(tmp_path):
    store, contract = _pg_case(tmp_path, states={"public.orders.trg_b": None})
    scripts = _cut(tmp_path, store, contract)
    assert (scripts.expected, scripts.in_script) == (1, 1)
    assert scripts.excluded == [("trigger", "public.orders.trg_b", "trigger_state_unknown")]
    assert '"trg_b"' not in _read(tmp_path, CUTOVER_FILE)
    assert "trg_b" not in _read(tmp_path, CUTOVER_CHECK_FILE)


def test_resume_is_mysql_only(tmp_path):
    store, contract = _pg_case(tmp_path)
    with pytest.raises(RoutineScriptError, match="one transaction"):
        _cut(tmp_path, store, contract, present=set())


def test_a_contract_written_before_the_section_is_an_error(tmp_path):
    with pytest.raises(RoutineScriptError, match="no routines section"):
        _cut(tmp_path, None, make_contract("aurora_postgresql"))


# --------------------------------------------------------------------------- Aurora MySQL


def _mysql_case(tmp_path, events=None, extra_rows=(), extra_sources=(), triggers=None):
    rows = [
        row("shop.place_order", "procedure", schema="shop", status="rewritten",
            signature="IN int", specific="place_order"),
        row("shop.orders_bi", "trigger", schema="shop", table="shop.orders",
            specific="orders.orders_bi", status="rewritten",
            dependencies=[dep_routine("procedure", "shop.place_order")]),
    ]  # fmt: skip
    sources = [
        mysql_source("place_order", "CREATE DEFINER=`app`@`%` PROCEDURE `place_order`(IN p INT)\n"
                     "BEGIN SELECT p; END"),
        mysql_source("orders.orders_bi", "CREATE DEFINER=`app`@`%` TRIGGER `orders_bi` BEFORE "
                     f"INSERT ON `orders` FOR EACH ROW SET NEW.note = '{SENTINEL}'", kind="trigger",
                     sql_mode="ANSI_QUOTES", collation="utf8mb4_bin"),
    ]  # fmt: skip
    for name, clause in (events or {}).items():
        rows.append(row(f"shop.{name}", "event", schema="shop", specific=name, status="rewritten"))
        sources.append(mysql_source(name, f"CREATE DEFINER=`app`@`%` EVENT `{name}` ON SCHEDULE "
                                    f"EVERY 1 DAY STARTS '2026-01-01 00:00:00' {clause} "
                                    "DO DELETE FROM orders WHERE id < 0", kind="event"))  # fmt: skip
    artifact = migration([*rows, *extra_rows], "mysql")
    store = _job(tmp_path, artifact, [*sources, *extra_sources], "mysql")
    return store, contract_with_routines(artifact, triggers=triggers)


def test_the_mysql_cutover_creates_the_deferred_triggers_and_events_as_the_deploy_writes_them(
    tmp_path,
):
    store, contract = _mysql_case(tmp_path, {"nightly": "ON COMPLETION PRESERVE ENABLE",
                                             "paused": "DISABLE COMMENT 'ENABLE later'",
                                             "replica": "DISABLE ON SLAVE"})  # fmt: skip
    scripts = _cut(tmp_path, store, contract, target_database="shop_new", target_user="migrator")
    assert (scripts.expected, scripts.in_script, scripts.events_enabled, scripts.excluded) == (
        4, 4, 1, [])  # fmt: skip
    text = _read(tmp_path, CUTOVER_FILE)
    body = text.split("SET NAMES utf8mb4;", 1)[1]
    assert "PROCEDURE" not in body  # step 1's deploy script created it
    assert "CREATE DEFINER=`migrator`@`%` TRIGGER `orders_bi`" in body and "`app`" not in body
    assert "SET SESSION sql_mode = 'ANSI_QUOTES';" in body
    assert "SET SESSION collation_connection = 'utf8mb4_bin';" in body
    assert "USE `shop_new`;" in body and SENTINEL in body
    assert body.index("`orders_bi`") < body.index("`nightly`") < body.index("`replica`")
    assert "-- 1/4 trigger shop.orders_bi" in body
    assert "1 of the events here are created enabled" in text
    assert scripts.events_replica_disabled == 1
    md = _read(tmp_path, CUTOVER_INSTRUCTIONS)
    assert "MySQL events fire on their schedule once created" in md and "DISABLE ON SLAVE" in md
    assert "`event_scheduler` is a parameter of your Aurora DB cluster parameter group" in md
    assert "cutover-scripts --resume" in md and "cutover-resume.sql" in md
    check = _read(tmp_path, CUTOVER_CHECK_FILE)
    assert "information_schema.EVENTS" in check and "information_schema.ROUTINES" not in check
    assert "UPPER(@@GLOBAL.event_scheduler)" in check
    assert "('shop_new', 'orders_bi')" in check and SENTINEL not in check + md


def test_a_deferred_routine_naming_a_routine_the_deploy_left_out_is_left_out(tmp_path):
    # The procedure's sql_mode is outside the allowlist: the deploy refuses it, so the
    # trigger that calls it is left out of the cutover too; the event stays.
    bad = mysql_source("place_order", "CREATE DEFINER=`app`@`%` PROCEDURE `place_order`(IN p INT)"
                       " BEGIN SELECT p; END", sql_mode="ANSI'; DROP")  # fmt: skip
    artifact = migration([
        row("shop.place_order", "procedure", schema="shop", status="rewritten", signature="IN int",
            specific="place_order"),
        row("shop.orders_bi", "trigger", schema="shop", table="shop.orders",
            specific="orders.orders_bi", status="rewritten",
            dependencies=[dep_routine("procedure", "shop.place_order")]),
    ], "mysql")  # fmt: skip
    store = _job(tmp_path, artifact, [bad, mysql_source(
        "orders.orders_bi", "CREATE DEFINER=`app`@`%` TRIGGER `orders_bi` BEFORE INSERT ON `orders` "
        "FOR EACH ROW SET NEW.id = NEW.id", kind="trigger")], "mysql")  # fmt: skip
    scripts = _cut(tmp_path, store, contract_with_routines(artifact))
    assert (scripts.in_script, scripts.excluded) == (
        0, [("trigger", "shop.orders_bi", "depends_on_refused")])  # fmt: skip
    assert "CREATE" not in _read(tmp_path, CUTOVER_FILE).split("SET NAMES utf8mb4;", 1)[1]


def test_all_mysql_routines_carried_means_nothing_to_do(tmp_path):
    artifact = migration([row("shop.q", "procedure", schema="shop", status="rewritten",
                              signature="", specific="q")], "mysql")  # fmt: skip
    store = _job(
        tmp_path, artifact, [mysql_source("q", "CREATE PROCEDURE `q`() SELECT 1")], "mysql"
    )
    assert _cut(tmp_path, store, contract_with_routines(artifact)).status == "nothing_to_do"


def test_the_printed_result_and_the_check_hold_no_routine_text(tmp_path):
    store, contract = _mysql_case(tmp_path, {"nightly": "ENABLE"})
    scripts = _cut(tmp_path, store, contract)
    printed = json.dumps(scripts.__dict__)
    assert SENTINEL not in printed and SENTINEL in _read(tmp_path, CUTOVER_FILE)
    assert SENTINEL not in _read(tmp_path, CUTOVER_CHECK_FILE)
    assert SENTINEL not in _read(tmp_path, CUTOVER_INSTRUCTIONS)
    leftovers = [p.name for p in (tmp_path / "step").iterdir()]
    assert sorted(leftovers) == sorted([CUTOVER_FILE, CUTOVER_CHECK_FILE, CUTOVER_INSTRUCTIONS])


def _two_triggers(fires=("BEFORE INSERT", "BEFORE INSERT"), orders=(2, 1)):
    rows = [row(f"shop.{n}", "trigger", schema="shop", table="shop.orders", specific=f"orders.{n}",
                status="rewritten") for n in ("first_bi", "second_bi")]  # fmt: skip
    sources = [mysql_source(f"orders.{n}", f"CREATE DEFINER=`app`@`%` TRIGGER `{n}` BEFORE INSERT "
                            "ON `orders` FOR EACH ROW SET NEW.id = NEW.id", kind="trigger")
               for n in ("first_bi", "second_bi")]  # fmt: skip
    triggers = {
        "shop.first_bi": {"fires": fires[0], "action_order": orders[0]},
        "shop.second_bi": {"fires": fires[1], "action_order": orders[1]},
        "shop.orders_bi": {"fires": "AFTER UPDATE", "action_order": 1},
    }
    return {"extra_rows": rows, "extra_sources": sources, "triggers": triggers}


def test_mysql_triggers_of_one_table_timing_and_event_are_created_in_their_action_order(tmp_path):
    store, contract = _mysql_case(tmp_path, **_two_triggers(orders=(2, 1)))
    scripts = _cut(tmp_path, store, contract)
    assert (scripts.in_script, scripts.excluded) == (3, [])
    body = _read(tmp_path, CUTOVER_FILE)
    # second_bi fires first on the source (ACTION_ORDER 1): MySQL fires in creation order.
    assert body.index("TRIGGER `second_bi`") < body.index("TRIGGER `first_bi`")


@pytest.mark.parametrize(
    "case",
    [
        {"orders": (1, None)},  # no ACTION_ORDER for one of the group
        {"orders": (1, 1)},  # two at one place
        {"fires": ("BEFORE INSERT", None)},  # one's timing and event unknown, on the same table
    ],
)
def test_mysql_triggers_whose_firing_order_is_unknown_are_left_out_never_guessed(tmp_path, case):
    store, contract = _mysql_case(tmp_path, **_two_triggers(**case))
    scripts = _cut(tmp_path, store, contract)
    left = {rid: reason for _, rid, reason in scripts.excluded}
    assert left["shop.first_bi"] == left["shop.second_bi"] == "trigger_order_unknown"
    # With one trigger's timing and event unknown, no trigger of that table has a known
    # place: orders_bi is left out too, and nothing is left to create.
    assert ("shop.orders_bi" in left) == ("fires" in case)
    path = tmp_path / "step" / CUTOVER_FILE
    body = path.read_text().split("SET NAMES", 1)[1] if path.exists() else ""
    assert "`first_bi`" not in body and "`second_bi`" not in body


def test_a_lone_trigger_or_different_events_need_no_action_order(tmp_path):
    store, contract = _mysql_case(
        tmp_path, **_two_triggers(fires=("BEFORE INSERT", "BEFORE UPDATE"), orders=(None, None))
    )
    assert _cut(tmp_path, store, contract).excluded == []


def test_resume_refuses_when_an_earlier_trigger_of_a_group_is_missing_and_a_later_one_is_there(
    tmp_path,
):
    store, contract = _mysql_case(tmp_path, **_two_triggers(orders=(2, 1)))
    # first_bi fires second on the source; second_bi fires first and is missing.
    present = {("trigger", "shop", "", "first_bi")}
    with pytest.raises(RoutineScriptError, match="cannot resume") as err:
        _cut(tmp_path, store, contract, present=present)
    assert "shop.first_bi" in str(err.value) and "shop.second_bi" in str(err.value)
    # The other way round appends in order: allowed.
    scripts = _cut(tmp_path, store, contract, present={("trigger", "shop", "", "second_bi")})
    assert (scripts.in_script, scripts.already_there) == (2, 1)


def test_resume_writes_only_what_the_check_shows_missing_in_the_same_order(tmp_path):
    from src.tools.data_movement.cutover_scripts import RESUME_FILE

    store, contract = _mysql_case(tmp_path, {"nightly": "ENABLE", "paused": "DISABLE"})
    _cut(tmp_path, store, contract, target_database="shop_new")
    present = {("trigger", "shop_new", "", "orders_bi")}
    scripts = _cut(tmp_path, store, contract, target_database="shop_new", present=present)
    assert (scripts.in_script, scripts.already_there, list(scripts.files)) == (2, 1, [RESUME_FILE])
    resume = _read(tmp_path, RESUME_FILE).split("SET NAMES utf8mb4;", 1)[1]
    assert "`orders_bi`" not in resume and resume.index("`nightly`") < resume.index("`paused`")
    assert "TRIGGER `orders_bi`" in _read(tmp_path, CUTOVER_FILE)  # the first script stays


@pytest.mark.parametrize(
    ("text", "state"),
    [
        ("CREATE EVENT e ON SCHEDULE EVERY 1 DAY DO SELECT 1", "ENABLED"),
        ("CREATE EVENT e ON SCHEDULE EVERY 1 DAY ENABLE DO SELECT 1", "ENABLED"),
        ("CREATE EVENT e ON SCHEDULE EVERY 1 DAY DISABLE DO SELECT 1", "DISABLED"),
        (
            "CREATE EVENT e ON SCHEDULE EVERY 1 DAY DISABLE ON SLAVE DO SELECT 1",
            "REPLICA_SIDE_DISABLED",
        ),
        (
            "CREATE EVENT e ON SCHEDULE EVERY 1 DAY DISABLE ON REPLICA DO SELECT 1",
            "REPLICA_SIDE_DISABLED",
        ),
        # Words inside quotes, comments or the body never count; neither does the event's name.
        (
            "CREATE EVENT `disable` ON SCHEDULE AT '2026-01-01' COMMENT 'DISABLE' DO SELECT 'DISABLE'",
            "ENABLED",
        ),
        (
            "CREATE EVENT enable ON SCHEDULE EVERY 1 DAY DISABLE COMMENT 'it''s ENABLE' DO SELECT 1",
            "DISABLED",
        ),
        ("CREATE EVENT e ON SCHEDULE EVERY 1 DAY /* DISABLE */ -- DISABLE\n DO DISABLE", "ENABLED"),
        ("CREATE EVENT e ON SCHEDULE EVERY 1 DAY COMMENT 'a\\' DISABLE' DO SELECT 1", "ENABLED"),
        ("CREATE EVENT e ON SCHEDULE EVERY 1 DAY", None),
    ],
)
def test_the_event_state_is_read_from_the_create_event_header_only(text, state):
    assert event_state(text) == state


def test_the_event_state_follows_no_backslash_escapes():
    text = "CREATE EVENT e ON SCHEDULE EVERY 1 DAY COMMENT 'a\\' DISABLE DO SELECT 1"
    assert event_state(text, "NO_BACKSLASH_ESCAPES") == "DISABLED"


def test_a_hundred_thousand_triggers_write_in_linear_time(tmp_path):
    def case(n: int):
        folder = tmp_path / str(n)
        folder.mkdir()
        return folder, _pg_case(folder, triggers=[f"t{i}" for i in range(n)])

    (big_dir, (big_store, big)), (ref_dir, (ref_store, ref)) = case(100_000), case(10_000)
    assert_cost_ratio(
        lambda: write_cutover_scripts(big_store, "shop", "job1", big, SHA, big_dir / "step"),
        lambda: write_cutover_scripts(ref_store, "shop", "job1", ref, SHA, ref_dir / "step"),
        25,
        max_rounds=3,
    )
    assert (big_dir / "step" / CUTOVER_FILE).read_text().count("ENABLE TRIGGER") == 100_000
