# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Side-by-side comparison on real disposable engines (#1944).

Known equal and unequal routine pairs, compared on two PostgreSQL 16 engines
(``pgserver``, a dev dependency, so these run in CI): a changed return value, a changed
table write, a changed trigger effect, an equal routine that reads the clock, a random
value, a UUID and a sequence, the could-not-compare path, and error equivalence. The
routines are fixtures written for this test, nothing collected.

The MySQL round trips are marked ``integration``: the gated CI run has no ``mysqld``.
"""

from __future__ import annotations

import json
import os

import pytest

from src.tools.database import disposable_engine as de
from src.tools.routines.comparison import WITHHELD, ComparedRoutine, compare_routines
from src.tools.routines.proof_inputs import RoutineProofInput
from src.tools.routines.proof_targets import disposable_target

needs_pg = pytest.mark.skipif(
    "16" not in de.available_majors("postgresql"), reason="pgserver (PostgreSQL 16) is needed"
)


def _as_root() -> bool:
    """One CI image runs as root, where every start is refused: assert that instead."""
    if os.geteuid() != 0:
        return False
    with pytest.raises(de.EngineStartError, match="running as root"):
        de.start("postgresql", "16")
    return True


def _col(name, data_type, nullable=False, **kw):
    return {"column_name": name, "data_type": data_type, "nullable": nullable, **kw}


TABLES = [
    {"table_id": "public.accounts", "table_name": "accounts", "row_count": 5,
     "primary_key": ["id"],
     "columns": [_col("id", "integer"),
                 _col("balance", "numeric(12,2)", min_value=0, max_value=1000),
                 _col("note", "text", True)]},
    {"table_id": "public.audit", "table_name": "audit", "row_count": 2, "primary_key": ["id"],
     "columns": [_col("id", "integer", is_auto_increment=True), _col("account_id", "integer", True),
                 _col("op", "text", True), _col("at", "timestamp with time zone", True),
                 _col("token", "uuid", True), _col("draw", "double precision", True)]},
]  # fmt: skip


def _proof(rid, kind, name, **kw):
    return RoutineProofInput(routine_id=rid, kind=kind, name=name, **kw)


def _pair(proof, original, carried, kind=None):
    kind = kind or proof.kind

    def routines(texts):
        if isinstance(texts, str):
            texts = [(proof.name, kind, texts)]
        return tuple(de.SourceRoutine(n, k, t) for n, k, t in texts)

    return ComparedRoutine(proof, routines(original), routines(carried))


def _compare(pairs, engine="postgresql", version="16", tables=TABLES):
    return compare_routines(pairs, tables, source=disposable_target(engine, version),
                            target=disposable_target(engine, version), dialect=engine)  # fmt: skip


def _tax(rate, name="add_tax"):
    return (f"CREATE FUNCTION {name}(n numeric) RETURNS numeric LANGUAGE sql "
            f"AS $$ SELECT round(n * {rate}, 2) $$")  # fmt: skip


TAX = _proof("public.add_tax(numeric)", "function", "add_tax", returns="numeric",
             parameters=[{"name": "n", "data_type": "numeric(8,2)"}])  # fmt: skip


def _credit(expr):
    return ("CREATE PROCEDURE credit(acc integer, amount numeric) LANGUAGE plpgsql AS $$ BEGIN "
            f"UPDATE accounts SET balance = {expr} WHERE id = acc; END $$")  # fmt: skip


CREDIT = _proof("public.credit(integer,numeric)", "procedure", "credit", tables=("accounts",),
                parameters=[{"name": "acc", "data_type": "integer"},
                            {"name": "amount", "data_type": "numeric(6,2)"}])  # fmt: skip


def _audit_trigger(expr):
    return [
        ("log_account", "function",
         "CREATE FUNCTION log_account() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
         f"INSERT INTO audit (account_id, op) VALUES ({expr}, TG_OP); RETURN NEW; END $$"),
        ("accounts_log", "trigger",
         "CREATE TRIGGER accounts_log AFTER INSERT OR UPDATE ON accounts "
         "FOR EACH ROW EXECUTE FUNCTION log_account()"),
    ]  # fmt: skip


TRIGGER = _proof("public.accounts.accounts_log", "trigger", "accounts_log", tables=("audit",),
                 trigger={"table": "accounts", "timing": "AFTER", "events": ["INSERT", "UPDATE"]})  # fmt: skip


@needs_pg
def test_known_equal_and_unequal_pairs_on_postgresql_16():
    """On one engine pair, an unchanged routine shows no differences and a changed return value is
    named by call, row, column and both values."""
    changed = _proof("public.add_tax2(numeric)", "function", "add_tax2", returns="numeric",
                     parameters=[{"name": "n", "data_type": "numeric(8,2)"}])  # fmt: skip
    pairs = [_pair(TAX, _tax("1.07"), _tax("1.07")),
             _pair(changed, _tax("1.07", "add_tax2"), _tax("1.08", "add_tax2"))]  # fmt: skip
    if _as_root():
        return
    same, changed = _compare(pairs).results
    assert same.outcome == "no_differences_found", same
    n = same.calls_compared
    assert same.reason == f"no differences found on {n} calls ({n} succeeded on both sides)"
    assert same.calls_compared >= 5
    assert changed.outcome == "different" and changed.status == "needs_attention"
    baseline = next(d for d in changed.differences if d.call == "baseline")
    assert (baseline.aspect, baseline.row, baseline.column) == ("result", {"row": 1}, "#1")
    assert (baseline.source, baseline.target) == ("1.07", "1.08")  # generated input 1


@needs_pg
def test_a_changed_table_write_on_postgresql_16():
    pairs = [_pair(CREDIT, _credit("balance + amount"), _credit("balance + amount + 1"))]
    if _as_root():
        return
    (credit,) = _compare(pairs).results
    assert credit.outcome == "different", credit
    # the first seeded row holds the column's highest value, so some calls overflow on the
    # carried side only (an error difference) and the others write one more
    assert {d.aspect for d in credit.differences} == {"table", "error"}
    d = next(d for d in credit.differences if d.aspect == "table")
    assert (d.table, d.change, d.row, d.column) == ("accounts", "written", {"id": 1}, "balance")
    assert float(d.target) - float(d.source) == pytest.approx(1)


@needs_pg
def test_a_changed_trigger_effect_on_postgresql_16():
    pairs = [_pair(TRIGGER, _audit_trigger("NEW.id"), _audit_trigger("NEW.id + 1000"))]
    if _as_root():
        return
    (trigger,) = _compare(pairs).results
    assert trigger.outcome == "different", trigger
    assert {(d.table, d.column) for d in trigger.differences} == {("audit", "account_id")}
    assert {d.call for d in trigger.differences} == {"insert", "update"}
    insert = next(d for d in trigger.differences if d.call == "insert")
    assert insert.target - insert.source == 1000


@needs_pg
def test_an_equal_routine_reading_now_random_and_a_sequence_shows_no_differences():
    """``now()`` (written as its exact offset), ``random()`` (frozen by ``setseed``, both
    sides on PostgreSQL 16) and an identity key (put back before each call) compare
    equal."""
    stamp = _proof("public.stamp(integer)", "function", "stamp", returns="timestamptz",
                   tables=("audit", "accounts"),
                   parameters=[{"name": "acc", "data_type": "integer"}])  # fmt: skip
    text = (
        "CREATE FUNCTION stamp(acc integer) RETURNS timestamptz LANGUAGE plpgsql AS $$ BEGIN "
        "INSERT INTO audit (account_id, op, at, draw) "
        "VALUES (acc, 'stamp', now(), random()); "
        "UPDATE accounts SET note = (now() + interval '2 days')::date::text WHERE id = acc; "
        "RETURN now(); END $$"
    )
    if _as_root():
        return
    (r,) = _compare([_pair(stamp, text, text)]).results
    assert (r.outcome, r.differences) == ("no_differences_found", ()), r


@needs_pg
def test_a_call_failing_on_one_side_does_not_shift_the_keys_of_the_calls_after_it():
    """The carried routine fails on ``n = 0`` before drawing an identity value, the
    original after drawing one. A rollback does not give a drawn value back, so without
    putting the counters back every later call would return keys one apart; only the
    failing call differs."""

    def log_it(guard):
        return ("CREATE FUNCTION log_it(n integer) RETURNS integer LANGUAGE plpgsql AS $$ "
                f"DECLARE k integer; BEGIN {guard} INSERT INTO audit (account_id) VALUES (n) "
                "RETURNING id INTO k; RETURN k; END $$")  # fmt: skip

    proof = _proof("public.log_it(integer)", "function", "log_it", returns="integer",
                   tables=("audit",), parameters=[{"name": "n", "data_type": "integer"}])  # fmt: skip
    if _as_root():
        return
    (r,) = _compare([_pair(proof, log_it(""),
                           log_it("IF n = 0 THEN RAISE division_by_zero; END IF;"))]).results  # fmt: skip
    assert r.outcome == "different"
    assert {d.call for d in r.differences} == {"n=zero"}, r.differences


@needs_pg
def test_an_original_that_does_not_load_could_not_be_compared_on_postgresql_16():
    """``plpython3u`` is not installed on the disposable engine: the original does not
    load, so the routine could not be compared, while its carried
    rewrite loads and the other routines are still compared."""
    original = ("CREATE FUNCTION add_tax(n numeric) RETURNS numeric LANGUAGE plpython3u "
                "AS $$ return n * 1.07 $$")  # fmt: skip
    pairs = [
        _pair(TAX, original, _tax("1.07")),
        _pair(CREDIT, _credit("balance + amount"), _credit("balance + amount")),
    ]
    if _as_root():
        return
    tax, credit = _compare(pairs).results
    assert (tax.outcome, tax.reason_code, tax.status) == ("could_not_compare", "original_does_not_load", "not_compared")  # fmt: skip
    assert "SQLSTATE" in tax.reason and "postgresql 16" in tax.reason
    assert "plpython3u" not in tax.reason  # the engine's message is never quoted
    assert credit.outcome == "no_differences_found"


@pytest.mark.passes_on_main  # main passes where no 9.6 is found; this pins it everywhere
def test_no_engine_of_the_source_version_could_not_compare(monkeypatch):
    """Hermetic (#2066): discovery finds no 9.6, whatever the runner has installed (a 9.6
    binary that cannot start would give ``engine_failed``). Other majors stay as found."""
    real = de.pg_candidates
    monkeypatch.setattr(
        de, "pg_candidates",
        lambda *a, **k: {m: v for m, v in real(*a, **k).items() if m != "9.6"},
    )  # fmt: skip
    (r,) = compare_routines([_pair(TAX, _tax("1.07"), _tax("1.07"))], TABLES,
                            source=disposable_target("postgresql", "9.6"),
                            target=disposable_target("postgresql", "16"),
                            dialect="postgresql").results  # fmt: skip
    assert (r.outcome, r.reason_code, r.status) == ("could_not_compare", "source_engine_unavailable", "not_compared")  # fmt: skip
    assert "no engine for postgresql 9.6" in r.reason


def _raises(name, body):
    return (f"CREATE FUNCTION {name}(n integer) RETURNS integer LANGUAGE plpgsql AS $$ BEGIN "
            f"IF n = 0 THEN {body}; END IF; RETURN n; END $$")  # fmt: skip


@needs_pg
def test_errors_match_on_the_full_sqlstate_on_postgresql_16():
    """``RETURN 1 / 0`` on both sides (22012, one message) is the same; 22012 against
    22003 (one class) and against P0001 are differences, shown by SQLSTATE and never by
    the message."""

    def proof(name):
        return _proof(f"public.{name}(integer)", "function", name, returns="integer",
                      parameters=[{"name": "n", "data_type": "integer"}])  # fmt: skip

    pairs = [
        _pair(proof("p1"), _raises("p1", "RETURN 1 / 0"), _raises("p1", "RETURN 1 / 0")),
        _pair(proof("p2"), _raises("p2", "RAISE division_by_zero"),
              _raises("p2", "RAISE numeric_value_out_of_range")),
        _pair(proof("p3"), _raises("p3", "RAISE division_by_zero"),
              _raises("p3", "RAISE EXCEPTION 'secret-%', n")),
    ]  # fmt: skip
    if _as_root():
        return
    p1, p2, p3 = _compare(pairs).results
    assert p1.outcome == "no_differences_found", p1
    assert p2.outcome == "different"
    assert [(d.source, d.target) for d in p2.differences] == [("SQLSTATE 22012", "SQLSTATE 22003")]
    assert p3.outcome == "different"
    (d,) = p3.differences
    assert (d.aspect, d.call, d.source, d.target) == ("error", "n=zero", "SQLSTATE 22012",
                                                      "SQLSTATE P0001")  # fmt: skip
    assert "secret" not in json.dumps(p3.model_dump())


@needs_pg
def test_a_value_holding_a_routine_literal_is_withheld_on_postgresql_16():
    def label(word):
        return (f"CREATE FUNCTION tier(n integer) RETURNS text LANGUAGE sql "
                f"AS $$ SELECT '{word}-' || n $$")  # fmt: skip

    tier = _proof("public.tier(integer)", "function", "tier", returns="text",
                  parameters=[{"name": "n", "data_type": "integer"}])  # fmt: skip
    if _as_root():
        return
    (r,) = _compare([_pair(tier, label("gold"), label("silver"))]).results
    assert r.outcome == "different"
    assert all(d.source.endswith(WITHHELD) and d.target.endswith(WITHHELD) for d in r.differences)
    assert all(d.source != d.target for d in r.differences)  # numbered per distinct value
    dumped = json.dumps(r.model_dump())
    assert "gold" not in dumped and "silver" not in dumped


@needs_pg
def test_a_procedure_that_commits_could_not_be_compared_on_postgresql_16():
    text = ("CREATE PROCEDURE credit(acc integer, amount numeric) LANGUAGE plpgsql AS $$ BEGIN "
            "UPDATE accounts SET balance = balance + amount WHERE id = acc; COMMIT; END $$")  # fmt: skip
    if _as_root():
        return
    (r,) = _compare([_pair(CREDIT, text, text)]).results
    assert (r.outcome, r.reason_code) == ("could_not_compare", "transaction_control")


@needs_pg
def test_counters_are_put_back_after_a_rollback_on_postgresql_16():
    if _as_root():
        return
    with de.running("postgresql", "16") as h:
        de.load(h, ["CREATE TABLE t (id integer GENERATED BY DEFAULT AS IDENTITY, v int)"])
        state = de.counters(h)
        first = de.execute(h, "INSERT INTO t (v) VALUES (1) RETURNING id").rows
        de.restore_counters(h, state)
        de.execute(h, "DELETE FROM t")
        again = de.execute(h, "INSERT INTO t (v) VALUES (1) RETURNING id").rows
        refused = de.execute(h, "INSERT INTO t (id, v) VALUES (1, 'x')")
    assert first == again == [[1]]
    assert refused.sqlstate == "22P02" and refused.error is not None


# ------------------------------------------------- reproductions from the review of fb7c8e67


def _no_args(name, returns, body, language="sql"):
    return (f"CREATE FUNCTION {name}() RETURNS {returns} LANGUAGE {language} "
            f"AS $$ {body} $$")  # fmt: skip


def _proof0(name, **kw):
    return _proof(f"public.{name}()", "function", name, returns="text", **kw)


@needs_pg
def test_errors_of_one_class_but_another_code_are_different_on_postgresql_16():
    """Review item 1: a unique violation (23505) against a not-null one (23502)."""
    proof = _proof0("put", tables=("accounts",))
    original = _no_args("put", "void", "INSERT INTO accounts (id, balance) VALUES (1, 0)")
    carried = _no_args("put", "void", "INSERT INTO accounts (id, balance) VALUES (1000, NULL)")
    if _as_root():
        return
    (r,) = _compare([_pair(proof, original, carried)]).results
    assert r.outcome == "different", r
    assert [(d.source, d.target) for d in r.differences] == [("SQLSTATE 23505", "SQLSTATE 23502")]


@needs_pg
def test_a_routine_whose_every_call_fails_on_both_sides_is_not_compared_on_postgresql_16():
    """Review item 2: both bodies read a table the setup never created (42P01)."""

    def body(op):
        return (f"CREATE FUNCTION calc(n integer) RETURNS integer LANGUAGE plpgsql AS $$ BEGIN "
                f"RETURN (SELECT count(*) FROM nosuch) {op} n; END $$")  # fmt: skip

    proof = _proof("public.calc(integer)", "function", "calc", returns="integer",
                   parameters=[{"name": "n", "data_type": "integer"}])  # fmt: skip
    if _as_root():
        return
    (r,) = _compare([_pair(proof, body("+"), body("-"))]).results
    assert (r.outcome, r.reason_code, r.calls_succeeded) == ("could_not_compare",
                                                             "table_not_observed", 0)  # fmt: skip


@needs_pg
def test_two_constant_uuids_differ_and_a_random_one_is_not_compared_on_postgresql_16():
    """Review item 4 and round 2, item 3: a constant UUID is never normalised; a routine
    calling ``gen_random_uuid()`` could not be compared."""

    def const(u):
        return _no_args("token", "uuid", f"SELECT '{u}'::uuid")

    fresh = _no_args("fresh", "text", "SELECT gen_random_uuid()::text || gen_random_uuid()::text")
    pairs = [_pair(_proof0("token"), const("1" * 8 + "-1111-1111-1111-" + "1" * 12),
                   const("2" * 8 + "-2222-2222-2222-" + "2" * 12)),
             _pair(_proof0("fresh"), fresh, fresh)]  # fmt: skip
    if _as_root():
        return
    token, generated = _compare(pairs).results
    assert token.outcome == "different", token
    assert (generated.outcome, generated.reason_code) == ("could_not_compare", "non_deterministic")
    assert "gen_random_uuid" in generated.reason


@needs_pg
def test_a_clock_offset_or_a_clock_type_change_is_different_on_postgresql_16():
    """Review item 5: ``now()`` against ``now() - interval '900 ms'``, and a
    ``timestamptz`` against a ``timestamp``."""
    pairs = [
        _pair(_proof0("t_offset"), _no_args("t_offset", "timestamptz", "SELECT now()"),
              _no_args("t_offset", "timestamptz", "SELECT now() - interval '900 ms'")),
        _pair(_proof0("t_type"), _no_args("t_type", "timestamptz", "SELECT now()"),
              _no_args("t_type", "timestamp", "SELECT now()::timestamp")),
        _pair(_proof0("t_same"), _no_args("t_same", "timestamptz", "SELECT current_timestamp"),
              _no_args("t_same", "timestamptz", "SELECT clock_timestamp()")),
    ]  # fmt: skip
    if _as_root():
        return
    offset, kind, clock = _compare(pairs).results
    assert offset.outcome == "different" and "-900000us" in offset.differences[0].target
    assert kind.outcome == "different", kind
    assert (clock.outcome, clock.reason_code) == ("could_not_compare", "non_deterministic")


@needs_pg
def test_two_routines_with_one_name_and_signature_are_both_compared_on_postgresql_16():
    """Review item 6: ``CREATE OR REPLACE`` of the same function by two routines; the
    changed one must not be compared as the other's bodies."""

    def tax(rate):
        return ("CREATE OR REPLACE FUNCTION dup(n numeric) RETURNS numeric LANGUAGE sql "
                f"AS $$ SELECT round(n * {rate}, 2) $$")  # fmt: skip

    def proof(rid):
        return _proof(rid, "function", "dup", returns="numeric",
                      parameters=[{"name": "n", "data_type": "numeric(8,2)"}])  # fmt: skip

    pairs = [_pair(proof("public.dup(numeric)#changed"), tax("1.07"), tax("1.08")),
             _pair(proof("public.dup(numeric)"), tax("1.10"), tax("1.10"))]  # fmt: skip
    if _as_root():
        return
    changed, same = _compare(pairs).results
    assert changed.outcome == "different", changed
    assert same.outcome == "no_differences_found", same


def _balance_trigger(name, expr):
    return [
        (f"{name}_fn", "function",
         f"CREATE FUNCTION {name}_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
         f"NEW.balance := {expr}; RETURN NEW; END $$"),
        (name, "trigger",
         f"CREATE TRIGGER {name} BEFORE UPDATE ON accounts FOR EACH ROW "
         f"EXECUTE FUNCTION {name}_fn()"),
    ]  # fmt: skip


@needs_pg
def test_one_trigger_never_masks_another_on_postgresql_16():
    """Review item 7: trigger ``a_bump`` (carried version adds 1) next to trigger
    ``b_fix``, which sets the balance on both sides after it."""

    def proof(name):
        return _proof(f"public.accounts.{name}", "trigger", name,
                      trigger={"table": "accounts", "timing": "BEFORE", "events": ["UPDATE"]})  # fmt: skip

    pairs = [_pair(proof("a_bump"), _balance_trigger("a_bump", "NEW.balance"),
                   _balance_trigger("a_bump", "NEW.balance + 1")),
             _pair(proof("b_fix"), _balance_trigger("b_fix", "0"), _balance_trigger("b_fix", "0"))]  # fmt: skip
    if _as_root():
        return
    bump, fix = _compare(pairs).results
    assert bump.outcome == "different", bump
    assert fix.outcome == "no_differences_found", fix


@needs_pg
def test_a_table_only_the_original_creates_is_different_on_postgresql_16():
    """Review item 9: a table a call creates is snapshot too."""
    proof = _proof0("make")
    original = _no_args("make", "void", "BEGIN CREATE TABLE made AS SELECT 1 AS v; END",
                        "plpgsql")  # fmt: skip
    carried = _no_args("make", "void", "BEGIN END", "plpgsql")
    if _as_root():
        return
    (r,) = _compare([_pair(proof, original, carried)]).results
    assert r.outcome == "different", r
    assert {d.table for d in r.differences} == {"made"}


@needs_pg
def test_a_call_past_its_time_limit_is_not_compared_and_the_others_still_run_on_postgresql_16():
    """Review item 10 and round 2, item 2: the carried routine loops forever on ``n = 0``,
    or both sides sleep past the limit: never a match."""

    def body(loop):
        return ("CREATE FUNCTION spin(n integer) RETURNS integer LANGUAGE plpgsql AS $$ BEGIN "
                f"IF n = 0 THEN {loop} END IF; RETURN n; END $$")  # fmt: skip

    spin = _proof("public.spin(integer)", "function", "spin", returns="integer",
                  parameters=[{"name": "n", "data_type": "integer"}])  # fmt: skip
    nap = _proof("public.nap(integer)", "function", "nap", returns="integer",
                 parameters=[{"name": "n", "data_type": "integer"}])  # fmt: skip

    def sleeps(value):
        return ("CREATE FUNCTION nap(n integer) RETURNS integer LANGUAGE plpgsql AS $$ BEGIN "
                f"PERFORM pg_sleep(3); RETURN {value}; END $$")  # fmt: skip

    pairs = [_pair(spin, body(""), body("LOOP PERFORM 1; END LOOP;")),
             _pair(nap, sleeps(1), sleeps(2)),
             _pair(TAX, _tax("1.07"), _tax("1.07"))]  # fmt: skip
    if _as_root():
        return
    report = compare_routines(pairs, TABLES, dialect="postgresql",
                              source=disposable_target("postgresql", "16", call_timeout_s=2),
                              target=disposable_target("postgresql", "16", call_timeout_s=2))  # fmt: skip
    looped, napped, tax = report.results
    assert (looped.outcome, looped.reason_code) == ("could_not_compare", "timeout"), looped
    assert (napped.outcome, napped.reason_code) == ("could_not_compare", "timeout"), napped
    assert tax.outcome == "no_differences_found"


@needs_pg
def test_errors_of_one_sqlstate_with_other_messages_or_constraints_differ_on_postgresql_16():
    """Round 2, item 1: two ``RAISE EXCEPTION`` messages (both P0001), and a duplicate key
    on ``accounts`` against one on ``audit`` (both 23505), each after a NULL argument
    returns early on both sides."""

    def raises(name, statement):
        return (f"CREATE FUNCTION {name}(n integer) RETURNS integer LANGUAGE plpgsql AS $$ "
                f"BEGIN IF n IS NULL THEN RETURN NULL; END IF; {statement}; RETURN n; END $$")  # fmt: skip

    def proof(name):
        return _proof(f"public.{name}(integer)", "function", name, returns="integer",
                      tables=("accounts", "audit"),
                      parameters=[{"name": "n", "data_type": "integer"}])  # fmt: skip

    pairs = [
        _pair(proof("msg"), raises("msg", "RAISE EXCEPTION 'insufficient funds'"),
              raises("msg", "RAISE EXCEPTION 'account locked'")),
        _pair(proof("dup"), raises("dup", "INSERT INTO accounts (id, balance) VALUES (1, 0)"),
              raises("dup", "INSERT INTO audit (id) VALUES (1)")),
    ]  # fmt: skip
    if _as_root():
        return
    msg, dup = _compare(pairs).results
    assert msg.outcome == "different", msg
    assert "insufficient" not in json.dumps(msg.model_dump())
    assert dup.outcome == "different", dup


@needs_pg
def test_local_and_utc_time_differ_under_the_session_time_zone_on_postgresql_16():
    """Round 2, item 8: the session runs at +05:45, so ``now()::timestamp`` (local) and
    ``now() AT TIME ZONE 'UTC'`` are told apart."""
    pairs = [_pair(_proof0("local"), _no_args("local", "timestamp", "SELECT now()::timestamp"),
                   _no_args("local", "timestamp", "SELECT now() AT TIME ZONE 'UTC'"))]  # fmt: skip
    if _as_root():
        return
    (r,) = _compare(pairs).results
    assert r.outcome == "different", r


def _typed_pair(name, returns_a, body_a, returns_b, body_b):
    return _pair(
        _proof0(name), _no_args(name, returns_a, body_a), _no_args(name, returns_b, body_b)
    )


@needs_pg
def test_values_keep_their_types_on_postgresql_16():
    """Round 3, item 1: each pair returns the same Python value through another type, and
    each is different; a ``jsonb`` of another key order is the same, a ``json`` is not."""
    pairs = [
        _typed_pair("t_bool", "boolean", "SELECT true", "integer", "SELECT 1"),
        _typed_pair("t_float", "integer", "SELECT 1", "double precision", "SELECT 1"),
        _typed_pair("t_text", "numeric", "SELECT 1.5", "text", "SELECT '1.5'"),
        _typed_pair("t_bytes", "bytea", "SELECT '\\x6869'::bytea", "text", "SELECT '6869'"),
        _typed_pair("t_month", "interval", "SELECT interval '1 mon'", "interval",
                    "SELECT interval '30 days'"),
        _typed_pair("t_day", "interval", "SELECT interval '1 day'", "interval",
                    "SELECT interval '24 hours'"),
        _typed_pair("t_jsonb", "jsonb", "SELECT jsonb_build_object('active', true)", "jsonb",
                    "SELECT jsonb_build_object('active', 1)"),
        _typed_pair("t_json", "json", 'SELECT \'{"a": 1, "b": 2}\'::json', "json",
                    'SELECT \'{"b": 2, "a": 1}\'::json'),
        _typed_pair("t_order", "jsonb", 'SELECT \'{"a": 1, "b": 2}\'::jsonb', "jsonb",
                    'SELECT \'{"b": 2, "a": 1}\'::jsonb'),
        _typed_pair("t_utc", "text", "SELECT to_char(now() AT TIME ZONE 'UTC', "
                    "'YYYY-MM-DD\"T\"HH24:MI:SS')", "text",
                    "SELECT to_char(now(), 'YYYY-MM-DD\"T\"HH24:MI:SS')"),
    ]  # fmt: skip
    if _as_root():
        return
    results = {r.routine_id: r for r in _compare(pairs).results}
    for name in ("t_bool", "t_float", "t_text", "t_bytes", "t_month", "t_day", "t_jsonb",
                 "t_json", "t_utc"):  # fmt: skip
        assert results[f"public.{name}()"].outcome == "different", results[f"public.{name}()"]
    assert results["public.t_order()"].outcome == "no_differences_found"


@needs_pg
def test_error_detail_hint_and_key_tell_errors_apart_on_postgresql_16():
    """Round 3, item 6: one ``RAISE`` with another DETAIL or HINT, and a duplicate key
    on another value of one constraint."""

    def raises(name, statement):
        return (f"CREATE FUNCTION {name}(n integer) RETURNS integer LANGUAGE plpgsql AS $$ "
                f"BEGIN IF n = 0 THEN {statement}; END IF; RETURN n; END $$")  # fmt: skip

    def proof(name):
        return _proof(f"public.{name}(integer)", "function", name, returns="integer",
                      tables=("accounts",),
                      parameters=[{"name": "n", "data_type": "integer"}])  # fmt: skip

    pairs = [
        _pair(proof("detail"), raises("detail", "RAISE EXCEPTION 'refused' USING DETAIL = 'limit 100'"),
              raises("detail", "RAISE EXCEPTION 'refused' USING DETAIL = 'limit 500'")),
        _pair(proof("hint"), raises("hint", "RAISE EXCEPTION 'refused' USING HINT = 'call us'"),
              raises("hint", "RAISE EXCEPTION 'refused' USING HINT = 'write to us'")),
        _pair(proof("key"), raises("key", "INSERT INTO accounts (id, balance) VALUES (1, 0)"),
              raises("key", "INSERT INTO accounts (id, balance) VALUES (2, 0)")),
    ]  # fmt: skip
    if _as_root():
        return
    for r in _compare(pairs).results:
        assert r.outcome == "different", r


@needs_pg
def test_a_named_zone_with_the_session_offset_is_told_apart_on_postgresql_16():
    """Round 3, item 4: the session's +05:47 is no named zone's offset, so local time
    against Kathmandu's (+05:45) differs."""
    pairs = [_typed_pair("k_local", "timestamp", "SELECT now()::timestamp", "timestamp",
                         "SELECT now() AT TIME ZONE 'Asia/Kathmandu'")]  # fmt: skip
    if _as_root():
        return
    (r,) = _compare(pairs).results
    assert r.outcome == "different", r


@needs_pg
def test_a_routine_touching_a_table_left_without_rows_is_not_compared_on_postgresql_16():
    """Round 3, item 5, and round 4, critical 1: the setup creates a table of a non-plain
    name but leaves it without rows, so an UPDATE of it would change nothing on either
    side: a routine touching it is not compared, however it writes to it."""
    odd = {"table_id": "public.odd log", "table_name": "odd log", "row_count": 2,
           "primary_key": ["id"], "columns": [_col("id", "integer"), _col("v", "integer", True)]}  # fmt: skip
    proof = _proof("public.touch(integer)", "function", "touch", returns="integer",
                   tables=("odd log",), parameters=[{"name": "n", "data_type": "integer"}])  # fmt: skip
    writes = ("CREATE FUNCTION touch(n integer) RETURNS integer LANGUAGE plpgsql AS $$ BEGIN "
              'INSERT INTO "odd log" (id, v) VALUES (100, 42); RETURN n; END $$')  # fmt: skip
    keeps = ("CREATE FUNCTION touch(n integer) RETURNS integer LANGUAGE plpgsql AS $$ BEGIN "
             "RETURN n; END $$")  # fmt: skip
    dynamic = ("CREATE FUNCTION touch(n integer) RETURNS integer LANGUAGE plpgsql AS $$ BEGIN "
               "EXECUTE format('UPDATE %I SET v = 42', 'odd log'); RETURN n; END $$")  # fmt: skip
    if _as_root():
        return
    updates = writes.replace(
        'INSERT INTO "odd log" (id, v) VALUES (100, 42)', 'UPDATE "odd log" SET v = 42'
    )
    pairs = [_pair(proof, text, keeps) for text in (writes, updates, dynamic)]
    for pair in pairs:
        (r,) = _compare([pair], tables=TABLES + [odd]).results
        assert (r.outcome, r.reason_code) == ("could_not_compare", "table_unseeded"), r


@needs_pg
def test_a_loop_that_catches_the_cancel_is_still_ended_on_postgresql_16():
    """Round 3, suggestion 16: after three cancels the backend is terminated."""
    spin = _proof("public.stubborn(integer)", "function", "stubborn", returns="integer",
                  parameters=[{"name": "n", "data_type": "integer"}])  # fmt: skip
    text = ("CREATE FUNCTION stubborn(n integer) RETURNS integer LANGUAGE plpgsql AS $$ BEGIN "
            "LOOP BEGIN PERFORM pg_sleep(0.05); EXCEPTION WHEN query_canceled THEN NULL; END; "
            "END LOOP; END $$")  # fmt: skip
    if _as_root():
        return
    report = compare_routines([_pair(spin, text, text), _pair(TAX, _tax("1.07"), _tax("1.07"))],
                              TABLES, dialect="postgresql",
                              source=disposable_target("postgresql", "16", call_timeout_s=1),
                              target=disposable_target("postgresql", "16", call_timeout_s=1))  # fmt: skip
    stubborn, tax = report.results
    assert (stubborn.outcome, stubborn.reason_code) == ("could_not_compare", "timeout"), stubborn
    assert tax.outcome == "no_differences_found", tax


@needs_pg
def test_cursors_column_names_session_and_sequences_on_postgresql_16():
    """Round 4: a ``refcursor`` is not compared; another result column name, a session
    setting left behind, and a sequence drawn or set on one side are differences; a
    routine reading the clock lists the real-time limit."""

    def cursor(expr):
        return ("CREATE FUNCTION cur(n integer) RETURNS refcursor LANGUAGE plpgsql AS $$ "
                f"DECLARE c refcursor; BEGIN OPEN c FOR SELECT {expr} FROM accounts; "
                "RETURN c; END $$")  # fmt: skip

    def table(column):
        return (f"CREATE FUNCTION named(n integer) RETURNS TABLE({column} integer) "
                "LANGUAGE sql AS $$ SELECT n $$")  # fmt: skip

    def body(name, statement):
        return (f"CREATE FUNCTION {name}(n integer) RETURNS integer LANGUAGE plpgsql AS $$ "
                f"BEGIN {statement} RETURN n; END $$")  # fmt: skip

    def proof(name, tables=()):
        return _proof(f"public.{name}(integer)", "function", name, returns="integer",
                      tables=tables, parameters=[{"name": "n", "data_type": "integer"}])  # fmt: skip

    pairs = [
        _pair(proof("cur", ("accounts",)), cursor("id"), cursor("id + 1")),
        _pair(proof("named"), table("total"), table("amount")),
        _pair(proof("dstyle"), body("dstyle", "SET datestyle = 'SQL, DMY';"), body("dstyle", "")),
        _pair(proof("seq", ("audit",)), body("seq", "PERFORM setval('public.audit_id_seq', 1000);"),
              body("seq", "")),
        _pair(proof("today"), body("today", "PERFORM current_date;"), body("today", "PERFORM current_date;")),
    ]  # fmt: skip
    if _as_root():
        return
    cur, named, dstyle, seq, today = _compare(pairs).results
    assert (cur.outcome, cur.reason_code) == ("could_not_compare", "refcursor"), cur
    assert named.outcome == "different" and named.differences[0].column == "names", named
    assert dstyle.outcome == "different", dstyle
    assert {d.aspect for d in dstyle.differences} == {"session"}
    assert seq.outcome == "different" and {d.aspect for d in seq.differences} == {"sequence"}, seq
    assert today.outcome == "no_differences_found", today
    assert any("real time" in c for c in today.coverage_limits)


@needs_pg
def test_a_create_outside_the_scratch_schema_is_refused_on_postgresql_16():
    """Review item 13: a carried ``CREATE FUNCTION app.f`` is never run."""
    carried = (
        "CREATE FUNCTION app.add_tax(n numeric) RETURNS numeric LANGUAGE sql AS $$ SELECT n $$"
    )
    if _as_root():
        return
    (r,) = _compare([_pair(TAX, _tax("1.07"), carried)]).results
    assert (r.outcome, r.reason_code) == ("could_not_compare", "outside_namespace")


# ---------------------------------------------------------------- MySQL (not in the gated run)

MY_TABLES = [
    {"table_id": "src.accounts", "table_name": "accounts", "row_count": 5, "primary_key": ["id"],
     "columns": [_col("id", "int"), _col("balance", "decimal(12,2)", min_value=0, max_value=1000),
                 _col("note", "text", True)]},
    {"table_id": "src.audit", "table_name": "audit", "row_count": 2, "primary_key": ["id"],
     "columns": [_col("id", "int", is_auto_increment=True), _col("account_id", "int", True),
                 _col("op", "varchar(20)", True), _col("at", "datetime", True),
                 _col("draw", "double", True)]},
]  # fmt: skip
MY_CREDIT = _proof("src.credit", "procedure", "credit", tables=("accounts", "audit"),
                   parameters=[{"name": "acc", "data_type": "int"},
                               {"name": "amount", "data_type": "decimal(6,2)"}])  # fmt: skip


def _my_credit(expr, name="credit"):
    return (f"CREATE PROCEDURE {name}(IN acc INT, IN amount DECIMAL(6,2)) BEGIN "
            f"UPDATE accounts SET balance = {expr} WHERE id = acc; "
            "INSERT INTO audit (account_id, op, at, draw) VALUES (acc, "
            "DATE_FORMAT(NOW(6) + INTERVAL 1 DAY, '%Y%m%d%H%i%s%f'), NOW(), RAND()); "
            "END")  # fmt: skip


@pytest.mark.integration
@pytest.mark.skipif(not de.available_majors("mysql"), reason="no mysqld binary")
def test_known_equal_and_unequal_pairs_on_mysql():
    """``NOW()`` (frozen by ``SET TIMESTAMP``, so a time derived from it and written as
    text compares equal too), ``RAND()`` (frozen by the session seeds)
    and an ``AUTO_INCREMENT`` key (put back before each call) compare equal; a changed
    write is named by table, row and column."""
    if os.geteuid() == 0:
        return
    version = de.available_majors("mysql")[0]
    changed = MY_CREDIT.model_copy(update={"routine_id": "src.credit2", "name": "credit2"})
    pairs = [_pair(MY_CREDIT, _my_credit("balance + amount"), _my_credit("balance + amount")),
             _pair(changed, _my_credit("balance + amount", "credit2"),
                   _my_credit("balance + amount + 1", "credit2"))]  # fmt: skip
    same, different = _compare(pairs, "mysql", version, MY_TABLES).results
    assert (same.outcome, same.differences) == ("no_differences_found", ()), same
    assert different.outcome == "different"
    d = next(d for d in different.differences if d.table == "accounts")
    assert (d.table, d.row, d.column) == ("accounts", {"id": 1}, "balance")


needs_mysql = pytest.mark.skipif(not de.available_majors("mysql"), reason="no mysqld binary")


def _my_proc(name, body, params="", tables=()):
    proof = _proof(f"src.{name}", "procedure", name, tables=tables,
                   parameters=[{"name": "n", "data_type": "int"}] if params else [])  # fmt: skip
    return proof, f"CREATE PROCEDURE {name}({params}) BEGIN {body} END"


def _my_compare(pairs, tables=MY_TABLES):
    return _compare(pairs, "mysql", de.available_majors("mysql")[0], tables).results


@pytest.mark.integration
@needs_mysql
def test_a_commit_on_one_side_only_is_different_on_mysql():
    """Review item 8: the original commits mid-procedure, the carried one does not; seen
    on the call it happens, the last one included."""
    if os.geteuid() == 0:
        return
    proof, original = _my_proc("pay", "UPDATE accounts SET note = 'x' WHERE id = 2; COMMIT;",
                               tables=("accounts",))  # fmt: skip
    _, carried = _my_proc("pay", "UPDATE accounts SET note = 'x' WHERE id = 2;")
    (r,) = _my_compare([_pair(proof, original, carried)])
    assert r.outcome == "different", r
    assert {(d.aspect, d.source, d.target) for d in r.differences} == {
        ("transaction", "committed", "open")}  # fmt: skip


@pytest.mark.integration
@needs_mysql
def test_session_state_is_reset_between_calls_on_mysql():
    """Review item 9: a user variable, a temporary table and a session setting one call
    leaves behind are gone before the next call."""
    if os.geteuid() == 0:
        return
    from src.tools.routines.comparison import frozen_at
    from src.tools.routines.proof_targets import DisposableEngineTarget

    target = DisposableEngineTarget("mysql", de.available_majors("mysql")[0])
    try:
        target.prepare_calls()
        h = target._handle
        target.begin_call(frozen_at(1), 0.5)
        de.execute(h, "SET @left_behind = 5")
        de.execute(h, "CREATE TEMPORARY TABLE scratch (v INT)")
        de.execute(h, "SET SESSION sql_mode = 'ANSI'")
        target.end_call()
        target.begin_call(frozen_at(2), 0.5)
        assert de.execute(h, "SELECT @left_behind").rows == [[None]]
        assert de.execute(h, "SELECT * FROM scratch").sqlstate == "42S02"
        assert "ANSI" not in de.execute(h, "SELECT @@SESSION.sql_mode").rows[0][0]
        assert de.execute(h, "SELECT @@SESSION.time_zone").rows == [["+05:47"]]
        target.end_call()
    finally:
        target.close()


@pytest.mark.integration
@needs_mysql
def test_a_table_a_call_creates_commits_and_is_seen_on_mysql():
    """Review item 9: DDL commits on MySQL, on both sides: could not compare."""
    if os.geteuid() == 0:
        return
    proof, text = _my_proc("make", "CREATE TABLE made (v INT);", tables=("accounts",))
    (r,) = _my_compare([_pair(proof, text, text)])
    # a table the routine's own text names but the setup lacks is not observed either
    assert r.outcome == "could_not_compare" and r.reason_code in ("commits", "table_not_observed")


@pytest.mark.integration
@needs_mysql
def test_moving_a_row_between_result_sets_is_different_on_mysql():
    """Review item 16."""
    if os.geteuid() == 0:
        return
    proof, two = _my_proc("sets", "SELECT 1 AS v; SELECT 2 AS v;")
    _, one = _my_proc("sets", "SELECT 1 AS v UNION ALL SELECT 2;")
    (r,) = _my_compare([_pair(proof, two, one)])
    assert r.outcome == "different", r
    sizes = next(d for d in r.differences if d.column is None and d.row is None)
    assert (sizes.source, sizes.target) == ([1, 1], [2])


@pytest.mark.integration
@needs_mysql
def test_an_auto_increment_table_with_a_non_plain_name_fails_no_other_routine_on_mysql():
    """Review item 14 and round 4, critical 1: its counter is put back with its name
    quoted, so the other routines are compared; a routine touching it is not, since the
    setup leaves it without rows."""
    if os.geteuid() == 0:
        return
    odd = {"table_id": "src.odd log", "table_name": "odd log", "row_count": 2,
           "primary_key": ["id"], "columns": [_col("id", "int", is_auto_increment=True),
                                              _col("v", "int", True)]}  # fmt: skip
    proof, text = _my_proc("plain", "SELECT n + 1;", "IN n INT", tables=("accounts",))
    touching, writes = _my_proc("touch", "INSERT INTO `odd log` (v) VALUES (42); SELECT n;", "IN n INT",
                                tables=("odd log",))  # fmt: skip
    _, keeps = _my_proc("touch", "SELECT n;", "IN n INT")
    plain, touch = _my_compare([_pair(proof, text, text), _pair(touching, writes, keeps)],
                               MY_TABLES + [odd])  # fmt: skip
    assert plain.outcome == "no_differences_found", plain
    assert (touch.outcome, touch.reason_code) == ("could_not_compare", "table_unseeded"), touch


@pytest.mark.integration
@needs_mysql
def test_a_commit_with_nothing_pending_or_an_implicit_one_is_seen_on_mysql():
    """Round 2, item 5: ``COMMIT`` with nothing else pending and a DDL statement's
    implicit commit (``ALTER TABLE``, which changes no row) each end the caller's
    transaction; the sentinel row shows it."""
    if os.geteuid() == 0:
        return
    proof, plain = _my_proc("c", "SELECT n;", "IN n INT", tables=("accounts",))
    pairs = [_pair(proof, _my_proc("c", "COMMIT; SELECT n;", "IN n INT")[1], plain)]
    other = proof.model_copy(update={"routine_id": "src.d", "name": "d"})
    pairs.append(_pair(other, _my_proc("d", "ALTER TABLE accounts COMMENT = 'x'; SELECT n;",
                                       "IN n INT")[1],
                       _my_proc("d", "SELECT n;", "IN n INT")[1]))  # fmt: skip
    for r in _my_compare(pairs):
        assert r.outcome == "different", r
        assert {d.aspect for d in r.differences} == {"transaction"}


@pytest.mark.integration
@needs_mysql
def test_signals_with_other_messages_and_utc_against_local_time_differ_on_mysql():
    """Round 2, items 1 and 8: two ``SIGNAL SQLSTATE '45000'`` texts, and ``NOW()``
    against ``UTC_TIMESTAMP()`` under the +05:45 session."""
    if os.geteuid() == 0:
        return

    def signal(text):
        return ("IF n IS NULL THEN SELECT n; ELSE SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = "
                f"'{text}'; END IF;")  # fmt: skip

    proof, original = _my_proc("sig", signal("insufficient funds"), "IN n INT")
    _, carried = _my_proc("sig", signal("account locked"), "IN n INT")
    clock, local = _my_proc("clk", "SELECT NOW(), n;", "IN n INT")
    _, utc = _my_proc("clk", "SELECT UTC_TIMESTAMP(), n;", "IN n INT")
    sig, clk = _my_compare([_pair(proof, original, carried), _pair(clock, local, utc)])
    assert sig.outcome == "different", sig
    assert clk.outcome == "different", clk


@pytest.mark.integration
@needs_mysql
def test_a_busy_or_sleeping_loop_is_stopped_and_not_compared_on_mysql():
    """Round 2, item 6: ``KILL CONNECTION``, repeated until the connection is gone, stops a
    busy loop and a loop of ``SLEEP``; the other routines of the batch still run."""
    if os.geteuid() == 0:
        return
    busy, spin = _my_proc("spin", "WHILE 1 DO SET @x = 1; END WHILE;")
    naps, nap = _my_proc("naps", "WHILE 1 DO SELECT SLEEP(1) INTO @slept; END WHILE;")
    plain, text = _my_proc("plain", "SELECT n + 1;", "IN n INT")
    version = de.available_majors("mysql")[0]
    report = compare_routines(
        [_pair(busy, spin, spin), _pair(naps, nap, nap), _pair(plain, text, text)], MY_TABLES,
        dialect="mysql", source=disposable_target("mysql", version, call_timeout_s=1),
        target=disposable_target("mysql", version, call_timeout_s=1),
    )  # fmt: skip
    spun, napped, ok = report.results
    assert (spun.outcome, spun.reason_code) == ("could_not_compare", "timeout"), spun
    assert (napped.outcome, napped.reason_code) == ("could_not_compare", "timeout"), napped
    assert ok.outcome == "no_differences_found", ok


@pytest.mark.integration
@needs_mysql
def test_the_time_limit_ends_a_sleeping_loop_and_the_handle_reconnects_on_mysql():
    """Round 2, item 6, on the engine itself."""
    if os.geteuid() == 0:
        return
    with de.running("mysql", de.available_majors("mysql")[0]) as h:
        de.load(h, [], {}, [de.SourceRoutine(
            "naps", "procedure", "CREATE PROCEDURE naps() BEGIN WHILE 1 DO "
            "SELECT SLEEP(1) INTO @slept; END WHILE; END")])  # fmt: skip
        with de.time_limit(h, 0.5) as limit:
            result = de.call(h, "naps")
        assert limit.fired and result.error is not None  # it returned, under the test's own limit
        assert de.execute(h, "SELECT 1").rows == [[1]]  # a new connection


@pytest.mark.integration
@needs_mysql
def test_a_rollback_microseconds_zones_and_types_are_told_apart_on_mysql():
    """Round 3, items 1 to 4 on MySQL: ``ROLLBACK`` and ``ROLLBACK AND CHAIN`` throw the
    caller's transaction away; ``NOW(6)`` against ``NOW()``; two named zones (loaded from
    the system's zoneinfo); local time against Kathmandu's; ``INT`` against ``DOUBLE``."""
    if os.geteuid() == 0:
        return

    def proc(name, body):
        return _my_proc(name, body, "IN n INT", tables=("accounts",))

    def fn(name, returns, expr):
        proof = _proof(f"src.{name}", "function", name, returns="text",
                       parameters=[{"name": "n", "data_type": "int"}])  # fmt: skip
        return proof, (f"CREATE FUNCTION {name}(n INT) RETURNS {returns} DETERMINISTIC "
                       f"RETURN {expr}")  # fmt: skip

    pairs = []
    for name, body in (("rb", "ROLLBACK; SELECT n;"), ("rbc", "ROLLBACK AND CHAIN; SELECT n;"),
                       ("fine", "SELECT NOW(6), n;"),
                       ("zone", "SELECT CONVERT_TZ(NOW(), '+05:45', 'Europe/Paris'), n;"),
                       ("kat", "SELECT NOW(), n;")):  # fmt: skip
        proof, original = proc(name, body)
        carried = {"rb": "SELECT n;", "rbc": "SELECT n;", "fine": "SELECT NOW(), n;",
                   "zone": "SELECT CONVERT_TZ(NOW(), '+05:45', 'America/New_York'), n;",
                   "kat": "SELECT CONVERT_TZ(UTC_TIMESTAMP(), '+00:00', '+05:45'), n;"}[name]  # fmt: skip
        pairs.append(_pair(proof, original, _my_proc(name, carried, "IN n INT")[1]))
    proof, original = fn("dt6", "DATETIME(6)", "NOW(6)")
    pairs.append(_pair(proof, original, fn("dt6", "DATETIME", "NOW(6)")[1]))
    proof, original = fn("num", "INT", "n")
    pairs.append(_pair(proof, original, fn("num", "DOUBLE", "n")[1]))
    for r in _my_compare(pairs):
        assert r.outcome == "different", r
    rb = _my_compare(pairs[:1])[0]
    assert {(d.source, d.target) for d in rb.differences if d.aspect == "transaction"} == {
        ("rolled back", "open")}  # fmt: skip


@pytest.mark.integration
@needs_mysql
def test_column_names_user_variables_and_counters_on_mysql():
    """Round 4: ``SELECT n AS total`` against ``AS amount``, a user variable left behind,
    and an ``AUTO_INCREMENT`` drawn on one side only."""
    if os.geteuid() == 0:
        return
    names, total = _my_proc("al", "SELECT n AS total;", "IN n INT")
    _, amount = _my_proc("al", "SELECT n AS amount;", "IN n INT")
    var, sets = _my_proc("uv", "SET @last_n = n; SELECT n;", "IN n INT")
    _, keeps = _my_proc("uv", "SELECT n;", "IN n INT")
    inc, draws = _my_proc("ai", "INSERT INTO audit (account_id) VALUES (n); "
                          "DELETE FROM audit WHERE account_id = n AND op IS NULL; SELECT n;",
                          "IN n INT", tables=("audit",))  # fmt: skip
    _, quiet = _my_proc("ai", "SELECT n;", "IN n INT")
    al, uv, ai = _my_compare([_pair(names, total, amount), _pair(var, sets, keeps),
                              _pair(inc, draws, quiet)])  # fmt: skip
    assert al.outcome == "different" and al.differences[0].column == "names", al
    assert uv.outcome == "different" and {d.aspect for d in uv.differences} == {"session"}, uv
    assert ai.outcome == "different" and "sequence" in {d.aspect for d in ai.differences}, ai


def test_the_mysql_round_trips_are_deselected_not_skipped_in_a_gated_run():
    for test in (test_known_equal_and_unequal_pairs_on_mysql,
                 test_a_commit_on_one_side_only_is_different_on_mysql,
                 test_session_state_is_reset_between_calls_on_mysql,
                 test_a_table_a_call_creates_commits_and_is_seen_on_mysql,
                 test_moving_a_row_between_result_sets_is_different_on_mysql,
                 test_an_auto_increment_table_with_a_non_plain_name_fails_no_other_routine_on_mysql,
                 test_a_commit_with_nothing_pending_or_an_implicit_one_is_seen_on_mysql,
                 test_signals_with_other_messages_and_utc_against_local_time_differ_on_mysql,
                 test_a_busy_or_sleeping_loop_is_stopped_and_not_compared_on_mysql,
                 test_the_time_limit_ends_a_sleeping_loop_and_the_handle_reconnects_on_mysql,
                 test_a_rollback_microseconds_zones_and_types_are_told_apart_on_mysql,
                 test_column_names_user_variables_and_counters_on_mysql):  # fmt: skip
        assert "integration" in {m.name for m in test.pytestmark}
