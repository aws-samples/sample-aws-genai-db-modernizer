# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The side-by-side comparison's diff logic, batching and outcomes, with no engine (#1944).

The diff logic (:func:`compare_call`, :func:`normalise`, :class:`ValueGuard`) is tested on
hand-made runs, the batch planner on routine metadata, and every outcome path on
:class:`FakeTarget`, an in-memory :class:`ProofTarget` whose routines are Python
functions. The rule they hold: when in doubt the outcome is ``different`` or
``could_not_compare``, never ``same``. The same paths on real engines are in
``test_routine_comparison_engine.py``.
"""

from __future__ import annotations

import copy
import datetime
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pytest

from src.tools.database import disposable_engine as de
from src.tools.routines.comparison import (
    WITHHELD,
    CallRun,
    ComparedRoutine,
    ValueGuard,
    call_seed,
    compare_call,
    compare_routines,
    frozen_at,
    normalise,
    plan_batches,
)
from src.tools.routines.proof_inputs import ProofCall, ProofSetup, RoutineProofInput
from src.tools.routines.proof_screen import (
    create_refusal,
    nondeterministic_calls,
    touched_tables,
)
from src.tools.routines.proof_targets import (
    CreateRefused,
    ProofTargetError,
    ProofTargetUnavailable,
)

UTC = datetime.UTC
T0 = datetime.datetime(2026, 11, 6, 10, 0, 0, tzinfo=UTC)
T1 = T0 + datetime.timedelta(seconds=7.25)  # the other side's transaction started later


def _run(rows=(), error=None, sqlstate=None, errno=None, before=None, after=None, now=T0,
         committed=False, result_sets=None, message="m", constraint=None,
         timed_out=False) -> CallRun:  # fmt: skip
    before = before if before is not None else {}
    after = after if after is not None else before
    result = de.CallResult(rows=[list(r) for r in rows], error=error, sqlstate=sqlstate,
                           errno=errno, result_sets=result_sets,
                           message=message if error is not None else None,
                           constraint=constraint, timed_out=timed_out)  # fmt: skip
    state = committed if isinstance(committed, str) else ("committed" if committed else "open")
    return CallRun(result, before, after, now, state)


def _w(value) -> bool:
    """Whether a value is shown withheld (``<withheld #n: holds routine text>``)."""
    return isinstance(value, str) and value.startswith("<withheld #") and value.endswith(WITHHELD)


def _diff(a: CallRun, b: CallRun, tables=(), keys=None, guard=None):
    return compare_call(a, b, call="baseline", index=1, tables=tables, keys=keys, guard=guard)


def _iso(moment: datetime.datetime, naive=False) -> str:
    return (moment.replace(tzinfo=None) if naive else moment).isoformat()


# --------------------------------------------------------------------------- normalise


def test_a_clock_value_is_written_as_its_exact_offset_from_the_calls_now():
    assert normalise(_iso(T0), T0) == "<now+0us as dddd-dd-ddTdd:dd:dd+00:00>"
    assert normalise(_iso(T1), T1) == normalise(_iso(T0), T0)  # each side's own now
    later = T0 + datetime.timedelta(days=1, microseconds=5)
    assert normalise(_iso(later), T0) == "<now+86400000005us as dddd-dd-ddTdd:dd:dd+00:00>"
    # one instant written at another offset keeps its offset in the form
    assert normalise("2026-11-06T15:47:00+05:47", T0) == "<now+0us as dddd-dd-ddTdd:dd:dd+05:47>"
    assert normalise("2026-11-06 10:00:00+00", T0) == "<now+0us as dddd-dd-dd dd:dd:dd+00>"
    assert normalise("2026-11-07", T0) == "<today+1d as dddd-dd-dd>"


def test_no_slack_hides_an_offset_and_the_type_is_kept():
    """Reproduction (review item 5): ``now()`` against ``now() - interval '900 ms'``, and a
    ``timestamptz`` against a ``timestamp`` of the same instant."""
    minus = T1 - datetime.timedelta(milliseconds=900)
    assert normalise(_iso(T0), T0) != normalise(_iso(minus), T1)
    assert normalise(_iso(T0), T0) != normalise(_iso(T1, naive=True), T1)
    (d,) = _diff(_run([[_iso(T0)]], now=T0), _run([[_iso(minus)]], now=T1))
    assert d.aspect == "result" and "-900000us" in d.target
    (d,) = _diff(_run([[_iso(T0)]], now=T0), _run([[_iso(T1, naive=True)]], now=T1))
    assert d.aspect == "result"


def test_far_generated_and_uuid_values_are_never_normalised():
    """Reproduction (review item 4): two different constant UUIDs differ."""
    assert normalise("2024-01-01", T0) == "2024-01-01"
    assert normalise(_iso(T0 + datetime.timedelta(days=8)), T0).startswith("2026-")
    assert normalise(_iso(T0), T0, {_iso(T0)}) == _iso(T0)
    assert normalise(_iso(T0), None) == _iso(T0)
    assert normalise(12, T0) == 12 and normalise(None, T0) is None
    one, two = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
    assert normalise(one, T0) == one
    (d,) = _diff(_run([[one]]), _run([[two]]))
    assert (d.source, d.target) == (one, two)
    assert normalise([_iso(T0), {"at": _iso(T0)}], T0)[1]["at"].startswith("<now+0us")


def test_freezing_is_the_same_per_call_on_both_sides_and_differs_between_calls():
    assert frozen_at(3) == frozen_at(3) != frozen_at(4)
    assert call_seed(0, 3) == call_seed(0, 3) != call_seed(0, 4)
    assert all(-1.0 <= call_seed(s, i) <= 1.0 for s in range(5) for i in range(200))


# --------------------------------------------------------------------------- the guard


def test_a_value_holding_a_routine_literal_is_withheld_and_a_generated_one_shown():
    text = "CREATE FUNCTION f(n int) RETURNS text LANGUAGE sql AS $$ SELECT 'gold-' || n $$"
    guard = ValueGuard.for_texts([text], "postgresql", generated={"gold-7"})
    assert _w(guard.shown("gold-1"))  # the literal inside the dollar-quoted body
    assert guard.shown("gold-7") == "gold-7"  # a generated value is fine
    assert guard.shown("plain") == "plain"
    assert guard.shown(42) == 42 and guard.shown("1.07") == "1.07"  # numbers are kept
    listed = guard.shown(["gold-2", {"k": "gold-3"}])
    assert _w(listed[0]) and _w(listed[1]["k"]) and listed[0] != listed[1]["k"]


def test_a_transformed_literal_is_withheld_too():
    """Reproduction (review item 11): ``upper()``, ``lower()`` and ``substr()`` of a
    literal, and any of its words of four or more characters, case-folded."""
    text = ("CREATE FUNCTION f() RETURNS text LANGUAGE sql "
            "AS $$ SELECT 'Confidential discount code' $$")  # fmt: skip
    guard = ValueGuard.for_texts([text], "postgresql", generated=set())
    for value in ("CONFIDENTIAL DISCOUNT CODE", "confidential discount code", "Confidential",
                  "Conf", "the DISCOUNT applies", "code: x"):  # fmt: skip
        assert _w(guard.shown(value)), value
    assert guard.shown("cod") == "cod"  # three characters of a literal: kept
    short = ValueGuard.for_texts(["CREATE FUNCTION g() AS $$ SELECT 'Ab' $$"], "postgresql", set())
    assert _w(short.shown("aB")) and short.shown("abc") == "abc"


def test_mysql_literals_and_punctuation_only_literals():
    text = "CREATE FUNCTION f(n INT) RETURNS TEXT RETURN CONCAT_WS('|', 'tier', n)"
    guard = ValueGuard.for_texts([text], "mysql", generated=set())
    assert _w(guard.shown("tier|1"))
    assert guard.shown("a|b") == "a|b"  # '|' has no letter or digit: redaction keeps it too


def test_a_text_that_does_not_tokenize_withholds_every_string_not_generated():
    guard = ValueGuard.for_texts(["CREATE FUNCTION f() AS $$ 'unterminated"], "postgresql",
                                 generated={"ok"})  # fmt: skip
    assert guard.strict
    assert _w(guard.shown("anything"))
    assert guard.shown("ok") == "ok" and _w(guard.shown(3))  # numbers too: fail closed


# --------------------------------------------------------------------------- one call


def test_equal_runs_have_no_difference():
    rows = {"t": [{"id": 1, "v": "a"}]}
    assert _diff(_run([[1, "x"]], before=rows), _run([[1, "x"]], before=rows), ["t"]) == []


def test_a_changed_return_value_names_the_row_the_column_and_both_values():
    (d,) = _diff(_run([[1, "1.07"]]), _run([[1, "1.08"]]))
    assert (d.aspect, d.call, d.call_index, d.row, d.column) == ("result", "baseline", 1,
                                                                 {"row": 1}, "#2")  # fmt: skip
    assert (d.source, d.target) == ("1.07", "1.08")


def test_a_missing_row_or_a_row_of_another_width_is_one_difference():
    (d,) = _diff(_run([[1], [2]]), _run([[1]]))
    assert (d.row, d.column, d.source, d.target) == ({"row": 2}, None, [2], None)
    (d,) = _diff(_run([[1, 2]]), _run([[1]]))
    assert (d.source, d.target) == ([1, 2], [1])


def test_result_sets_are_compared_by_their_sizes():
    """Review item 16: a row moved from one MySQL result set to another differs."""
    (d,) = _diff(_run([[1], [2]], result_sets=(1, 1)), _run([[1], [2]], result_sets=(2,)))
    assert (d.aspect, d.source, d.target) == ("result", [1, 1], [2])


def test_errors_match_only_on_the_full_sqlstate():
    """Reproduction (review item 1): 23505 (unique) against 23502 (not null) differs."""
    assert _diff(_run(error="x", sqlstate="22012"), _run(error="y", sqlstate="22012")) == []
    (d,) = _diff(_run(error="x", sqlstate="23505"), _run(error="y", sqlstate="23502"))
    assert (d.aspect, d.source, d.target) == ("error", "SQLSTATE 23505", "SQLSTATE 23502")


def test_mysql_errors_match_on_the_error_number_too():
    a = _run(error="x", sqlstate="HY000", errno=1205)
    assert _diff(a, _run(error="y", sqlstate="HY000", errno=1205)) == []
    (d,) = _diff(a, _run(error="y", sqlstate="HY000", errno=3024))
    assert (d.source, d.target) == ("SQLSTATE HY000 (error 1205)", "SQLSTATE HY000 (error 3024)")


def test_errors_of_one_sqlstate_match_only_on_their_message_and_constraint():
    """Round 2, item 1: ``RAISE EXCEPTION 'insufficient funds'`` against
    ``'account locked'`` (both P0001), and a duplicate key on two different tables (both
    23505), differ; neither message nor constraint is shown."""
    a = _run(error="x", sqlstate="P0001", message="insufficient funds")
    (d,) = _diff(a, _run(error="x", sqlstate="P0001", message="account locked"))
    assert (d.source, d.target) == (
        "SQLSTATE P0001 (one message)",
        "SQLSTATE P0001 (another message)",
    )
    assert "funds" not in json.dumps(d.model_dump())
    assert _diff(a, _run(error="y", sqlstate="P0001", message="insufficient   funds")) == []
    k1 = _run(error="x", sqlstate="23505", message="dup", constraint="accounts_pkey.accounts")
    k2 = _run(error="x", sqlstate="23505", message="dup", constraint="audit_pkey.audit")
    assert len(_diff(k1, k2)) == 1 and _diff(k1, k1) == []
    assert len(_diff(_run(error="x", sqlstate="45000", errno=1644, message="a"),
                     _run(error="x", sqlstate="45000", errno=1644, message="b"))) == 1  # fmt: skip


def test_an_error_with_no_message_or_a_timeout_never_matches():
    assert len(_diff(_run(error="x", sqlstate="P0001", message=None),
                     _run(error="x", sqlstate="P0001", message=None))) == 1  # fmt: skip
    (d,) = _diff(_run(error="t", timed_out=True), _run(error="t", timed_out=True))
    assert "timed out" in d.source


def test_two_errors_with_no_sqlstate_never_match():
    """Reproduction (review item 3)."""
    (d,) = _diff(_run(error="harness"), _run(error="harness"))
    assert d.aspect == "error"


def test_an_error_on_one_side_or_a_timeout_is_a_difference_shown_by_sqlstate_only():
    a = _run(error="RaiseException: secret text", sqlstate="P0001")
    (d,) = _diff(a, _run([[1]]))
    assert (d.source, d.target) == ("SQLSTATE P0001", "no error")
    assert "secret" not in json.dumps(d.model_dump())
    (d,) = _diff(_run([[1]]), _run(error="QueryCanceled: x", sqlstate="57014"))
    assert d.target == "SQLSTATE 57014 (timed out)"


def test_a_commit_on_one_side_only_is_a_difference():
    (d,) = _diff(_run([[1]], committed=True), _run([[1]]))
    assert (d.aspect, d.source, d.target) == ("transaction", "committed", "open")
    (d,) = _diff(_run([[1]]), _run([[1]], committed="rolled back"))
    assert (d.source, d.target) == ("open", "rolled back")


def test_a_changed_table_write_is_paired_by_key_and_named_by_column():
    before = {"acc": [{"id": 1, "bal": "10"}, {"id": 2, "bal": "5"}]}
    a = _run(before=before, after={"acc": [{"id": 1, "bal": "11"}, {"id": 2, "bal": "5"}]})
    b = _run(before=before, after={"acc": [{"id": 1, "bal": "12"}, {"id": 2, "bal": "5"}]})
    (d,) = _diff(a, b, ["acc"], keys={"acc": ["id"]})
    assert (d.aspect, d.table, d.change, d.row, d.column) == ("table", "acc", "written",
                                                              {"id": 1}, "bal")  # fmt: skip
    assert (d.source, d.target) == ("11", "12")


def test_a_row_written_or_removed_on_one_side_only():
    before = {"log": [{"id": 1, "op": "x"}]}
    a = _run(before=before, after={"log": [{"id": 1, "op": "x"}, {"id": 2, "op": "y"}]})
    (d,) = _diff(a, _run(before=before), ["log"], keys={"log": ["id"]})
    assert (d.change, d.row, d.column, d.source, d.target) == (
        "written", {"id": 2}, None, {"id": 2, "op": "y"}, None)  # fmt: skip
    b = _run(before=before, after={"log": []})
    (d,) = _diff(_run(before=before), b, ["log"], keys={"log": ["id"]})
    assert (d.change, d.source, d.target) == ("removed", None, {"id": 1, "op": "x"})


def test_a_table_only_one_side_created_is_compared():
    """Review item 9: a table the call creates is in the snapshot after it."""
    a = _run(before={"t": []}, after={"t": [], "made": [{"v": 1}]})
    (d,) = _diff(a, _run(before={"t": []}), ["t"])
    assert (d.table, d.source) == ("made", {"v": 1})


def test_without_a_key_rows_pair_by_most_equal_columns():
    before: dict[str, list[dict[str, Any]]] = {"t": []}
    a = _run(before=before, after={"t": [{"a": 1, "b": 2, "c": 3}]})
    b = _run(before=before, after={"t": [{"a": 1, "b": 2, "c": 4}]})
    (d,) = _diff(a, b, ["t"])
    assert (d.row, d.column, d.source, d.target) == ({"a": 1, "b": 2, "c": 3}, "c", 3, 4)


def test_table_changes_are_multisets():
    before: dict[str, list[dict[str, Any]]] = {"t": []}
    once = _run(before=before, after={"t": [{"v": 1}]})
    twice = _run(before=before, after={"t": [{"v": 1}, {"v": 1}]})
    (d,) = _diff(twice, once, ["t"])
    assert (d.change, d.source, d.target) == ("written", {"v": 1}, None)


def test_a_clock_value_written_on_each_side_compares_equal_at_the_same_offset():
    before = {"audit": [{"id": 1, "at": "2024-01-01T00:00:00+00:00"}]}

    def wrote(now, offset):
        row = {"id": 2, "at": _iso(now + offset)}
        return _run(before=before, now=now, after={"audit": before["audit"] + [row]})

    zero, day = datetime.timedelta(0), datetime.timedelta(days=1)
    assert _diff(wrote(T0, zero), wrote(T1, zero), ["audit"], keys={"audit": ["id"]}) == []
    assert _diff(wrote(T0, day), wrote(T1, day), ["audit"], keys={"audit": ["id"]}) == []
    (d,) = _diff(wrote(T0, zero), wrote(T1, day), ["audit"], keys={"audit": ["id"]})
    assert d.column == "at" and d.source.startswith("<now+0us")


def test_differing_values_holding_a_literal_are_withheld_in_every_aspect():
    text = "CREATE PROCEDURE p() BEGIN INSERT INTO t (v) VALUES ('approved'); END"
    guard = ValueGuard.for_texts([text], "mysql", generated=set())
    before: dict[str, list[dict[str, Any]]] = {"t": []}
    a = _run([["approved"]], before=before, after={"t": [{"id": 1, "v": "approved"}]})
    b = _run([["APPROVED!"]], before=before, after={"t": [{"id": 1, "v": "APPROVED!"}]})
    found = _diff(a, b, ["t"], keys={"t": ["id"]}, guard=guard)
    assert {d.aspect for d in found} == {"result", "table"}
    assert "approved" not in json.dumps([d.model_dump() for d in found]).casefold()
    assert all(_w(d.source) and _w(d.target) for d in found)
    assert all(d.source != d.target for d in found)  # numbered per distinct value


# --------------------------------------------------------------------------- namespace


def test_a_create_naming_another_schema_is_refused():
    """Review item 13 and round 2, item 7: a carried ``CREATE FUNCTION app.f`` never
    writes outside the target's namespace."""
    pg, my = "postgresql", "mysql"

    def pg_fn(text):
        return create_refusal(text, pg, "function", "public")

    def my_proc(text):
        return create_refusal(text, my, "procedure", "src")

    body = "RETURNS int LANGUAGE sql AS $$ SELECT 1 $$"
    assert pg_fn(f"CREATE FUNCTION app.f() {body}")
    assert pg_fn(f"CREATE OR REPLACE FUNCTION public.f() {body}") is None
    assert pg_fn(f"CREATE FUNCTION f() {body}") is None
    assert pg_fn(f'CREATE FUNCTION "Public".f() {body}')  # quoted: another schema
    assert create_refusal("CREATE TRIGGER t AFTER INSERT ON app.t FOR EACH ROW "
                          "EXECUTE FUNCTION f()", pg, "trigger", "public")  # fmt: skip
    assert my_proc("CREATE DEFINER=`a`@`%` PROCEDURE app.p() BEGIN END")
    assert my_proc("CREATE PROCEDURE src.p() BEGIN END") is None
    assert my_proc("CREATE PROCEDURE SRC.p() BEGIN END")  # MySQL: compared exactly
    assert pg_fn("not a create")


def test_more_than_one_statement_or_an_odd_header_is_refused():
    """Round 2, item 7."""
    pg, my = "postgresql", "mysql"
    body = "RETURNS int LANGUAGE sql AS $$ SELECT 1 $$"
    for extra in ("; CREATE SCHEMA app", "; ALTER FUNCTION f() SET SCHEMA app"):
        assert "exactly one" in create_refusal(f"CREATE FUNCTION f() {body}{extra}", pg,
                                               "function", "public")  # fmt: skip
    assert "exactly one" in create_refusal(
        "CREATE PROCEDURE p() BEGIN SELECT 1; END; CREATE PROCEDURE q() BEGIN END", my,
        "procedure", "src")  # fmt: skip
    assert create_refusal("CREATE DEFINER=`function`@`%` PROCEDURE other.p() BEGIN END", my,
                          "procedure", "src")  # fmt: skip
    assert create_refusal("CREATE DEFINER = PROCEDURE p() BEGIN END", my, "procedure", "src")
    for definer in ("`u`@`h`", "'u'@'h'", "root@localhost", "CURRENT_USER", "CURRENT_USER()"):
        assert create_refusal(f"CREATE DEFINER={definer} PROCEDURE p() BEGIN END", my,
                              "procedure", "src") is None, definer  # fmt: skip


def test_search_path_and_other_schemas_in_a_body_are_refused():
    """Round 2, item 7: a ``SET search_path`` clause or statement, ``set_config``, and a
    body that writes another schema's table."""
    pg = "postgresql"

    def refusal(text):
        return create_refusal(text, pg, "function", "public")

    assert "search_path" in refusal("CREATE FUNCTION f() RETURNS int LANGUAGE sql "
                                    "SET search_path = app AS $$ SELECT 1 $$")  # fmt: skip
    assert "search_path" in refusal("CREATE FUNCTION f() RETURNS void LANGUAGE plpgsql AS $$ "
                                    "BEGIN SET search_path = app; END $$")  # fmt: skip
    assert refusal("CREATE FUNCTION f() RETURNS text LANGUAGE sql AS $$ "
                   "SELECT set_config('search_path', 'app', true) $$")  # fmt: skip
    assert "other than" in refusal("CREATE FUNCTION f() RETURNS void LANGUAGE plpgsql AS $$ BEGIN "
                                "IF true THEN INSERT INTO app.accounts VALUES (1); END IF; END $$")  # fmt: skip
    assert refusal("CREATE FUNCTION f() RETURNS int LANGUAGE sql AS $$ "
                   "SELECT count(*)::int FROM pg_catalog.pg_class $$") is None  # fmt: skip
    assert create_refusal("CREATE PROCEDURE p() BEGIN UPDATE other.t SET v = 1; END", "mysql",
                          "procedure", "src")  # fmt: skip


def test_nondeterministic_calls_and_touched_tables_are_read_from_the_tokens():
    """Round 2, item 3: what the comparison cannot freeze is named, body included;
    ``random`` only when the two sides cannot seed it alike."""
    pg = ["CREATE FUNCTION f() RETURNS text LANGUAGE plpgsql AS $$ BEGIN "
          "RETURN clock_timestamp()::text || gen_random_uuid() || random(); END $$"]  # fmt: skip
    assert nondeterministic_calls(pg, "postgresql", random_seeded=True) == (
        "clock_timestamp", "gen_random_uuid")  # fmt: skip
    assert "random" in nondeterministic_calls(pg, "postgresql", random_seeded=False)
    my = ["CREATE PROCEDURE p() BEGIN SELECT UUID(), SYSDATE(), RAND(), NOW(); END"]
    assert nondeterministic_calls(my, "mysql", random_seeded=True) == ("sysdate", "uuid")
    plain = ["CREATE FUNCTION f() RETURNS timestamptz LANGUAGE sql AS $$ SELECT now() $$"]
    assert nondeterministic_calls(plain, "postgresql", random_seeded=True) == ()
    assert touched_tables("CREATE FUNCTION f() RETURNS int LANGUAGE plpgsql AS $$ BEGIN "
                          "PERFORM 1 FROM nosuch; RETURN 1; END $$", "postgresql",
                          "function") == ("nosuch",)  # fmt: skip


# --------------------------------------------------------------------------- batches


def _proof(rid, kind="function", name=None, params=("integer",), **kw):
    return RoutineProofInput(routine_id=rid, kind=kind, name=name or rid,
                             parameters=[{"name": f"p{i}", "data_type": t}
                                         for i, t in enumerate(params)], **kw)  # fmt: skip


def _plain(proof, helpers=()):
    statements = (de.SourceRoutine(proof.name, proof.kind, "x"),) + tuple(
        de.SourceRoutine(n, "function", "x") for n in helpers
    )
    return ComparedRoutine(proof, statements, statements)


def _trigger(rid, table, touches=(), helper=None):
    proof = _proof(rid, "trigger", params=(), tables=touches,
                   trigger={"table": table, "timing": "BEFORE", "events": ["UPDATE"]})  # fmt: skip
    return _plain(proof, (helper,) if helper else ())


def _ids(batches):
    return [[r.routine_id for r in b] for b in batches]


def test_two_routines_with_one_name_and_signature_never_share_a_batch():
    """Reproduction (review item 6): the second ``CREATE OR REPLACE`` would replace the
    first. Overloads, which do not collide, still share one."""
    rs = [_plain(_proof("a#1", name="dup", params=("numeric",))),
          _plain(_proof("a#2", name="public.dup", params=("NUMERIC",))),
          _plain(_proof("a#3", name="dup", params=("text",)))]  # fmt: skip
    batches = [set(b) for b in _ids(plan_batches(rs, batch_size=10, dialect="postgresql"))]
    assert len(batches) == 2 and not any({"a#1", "a#2"} <= b for b in batches)
    # MySQL has no overloads
    assert len(plan_batches(rs, batch_size=10, dialect="mysql")) == 3
    assert len(plan_batches(rs[:1] + rs[2:], batch_size=10, dialect="mysql")) == 2


def test_every_trigger_is_alone_in_its_batch():
    """Review item 7 and round 2, item 4: no trigger fires on another routine's calls,
    whatever its helpers write."""
    rs = [_plain(_proof("f")), _plain(_proof("g")), _trigger("t1", "accounts"),
          _trigger("t2", "orders"), _trigger("t3", "items", helper="log_fn")]  # fmt: skip
    batches = _ids(plan_batches(rs, batch_size=10, dialect="postgresql"))
    assert batches == [["f", "g"], ["t1"], ["t2"], ["t3"]]


def test_routines_sharing_a_dependency_share_a_batch_and_size_is_kept():
    helper = _plain(_proof("h"))
    users = [ComparedRoutine(_proof(f"u{i}"), (de.SourceRoutine(f"u{i}", "function", "x"),),
                             (de.SourceRoutine(f"u{i}", "function", "x"),), ("h",))
             for i in range(3)]  # fmt: skip
    assert _ids(plan_batches([helper, *users], batch_size=10, dialect="postgresql")) == [
        ["h", "u0", "u1", "u2"]]  # fmt: skip
    assert [len(b) for b in plan_batches([helper, *users], batch_size=3, dialect="postgresql")] == [
        3,
        1,
    ]


# --------------------------------------------------------------------------- the fake target

Routine = Callable[[dict[str, list[dict[str, Any]]], ProofCall], de.CallResult]


class FakeTarget:
    """An in-memory target. ``routines`` maps a routine's ``CREATE`` text to its Python
    body; a text not in it does not load (SQLSTATE 42601), and one starting with
    ``CREATE app.`` is refused. A row change appends a row. ``commits`` names routines
    whose effects survive the rollback."""

    def __init__(self, engine="postgresql", version="16", routines=None, commits=(), notes=(),
                 rollbacks=()):  # fmt: skip
        self.engine, self.version = engine, version
        self.description = f"a fake {engine} {version}"
        self.namespace = "public"
        self.random_seeded = True
        self.notes = tuple(notes)
        self.routines: Mapping[str, Routine] = routines or {}
        self.loaded: dict[str, Routine] = {}
        self.commits = set(commits)
        self.rollbacks = set(rollbacks)
        self.data: dict[str, list[dict[str, Any]]] = {}
        self.saved: dict[str, list[dict[str, Any]]] | None = None
        self.closed = False

    def load_setup(self, setup: ProofSetup) -> None:
        self.data = {t.rpartition(".")[2]: [] for t in setup.tables}
        for table, rows in setup.rows:
            self.data[table] = [dict(r) for r in rows]

    def create(self, routine: de.SourceRoutine) -> str | None:
        if routine.create_sql.startswith("CREATE app."):
            raise CreateRefused("its name is qualified with a schema other than the target's")
        body = self.routines.get(routine.create_sql)
        if body is None:
            return "42601"
        self.loaded[routine.name] = body
        return None

    def prepare_calls(self) -> None:
        pass

    def begin_call(self, frozen_at, seed) -> None:
        self.saved = copy.deepcopy(self.data)

    def now(self) -> datetime.datetime:
        return T0

    def tables(self) -> list[str]:
        return sorted(self.data)

    named_time_zones = True

    def transaction_state(self) -> str:
        if self._rolled_back:
            return "rolled back"
        return "committed" if self.saved is not None and self._committed else "open"

    def column_types(self, tables):
        return {t: {"id": "integer"} for t in tables}

    session: dict[str, str] | None = None  # None: the engine does not let it be read

    def session_state(self):
        return None if self.session is None else dict(self.session)

    def counter_state(self):
        return {"acc_id_seq": str(len(self.data.get("acc", [])))}

    _committed = False
    _rolled_back = False

    def run(self, call: ProofCall) -> de.CallResult:
        if call.action == "row_change":
            self.data[call.name].append(dict(zip(call.columns, call.args, strict=True)))
            return de.CallResult()
        self._committed = False
        self._rolled_back = call.name in self.rollbacks
        result = self.loaded[call.name](self.data, call)
        if call.name in self.commits:
            self.saved = copy.deepcopy(self.data)
            self._committed = True
        return result

    def snapshot(self, tables: Sequence[str]) -> Mapping[str, list[dict[str, Any]]]:
        return {t: sorted(self.data.get(t, []), key=repr) for t in tables}

    def end_call(self) -> None:
        if self.saved is not None:
            self.data, self.saved = self.saved, None

    def close(self) -> None:
        self.closed = True


def _col(name, data_type, nullable=False, **kw):
    return {"column_name": name, "data_type": data_type, "nullable": nullable, **kw}


TABLES = [{"table_id": "public.acc", "table_name": "acc", "row_count": 3, "primary_key": ["id"],
           "columns": [_col("id", "integer"), _col("bal", "integer")]},
          {"table_id": "public.odd", "table_name": "odd name", "row_count": 3,
           "columns": [_col("id", "integer")]}]  # fmt: skip


def _fn(rid, *, tables=("acc",)):
    return RoutineProofInput(routine_id=rid, kind="function", name=rid, returns="integer",
                             tables=tables, parameters=[{"name": "n", "data_type": "integer"}])  # fmt: skip


def _returns(f):
    def body(tables, call):
        n = call.args[0]
        return de.CallResult(rows=[[f(int(n))]]) if n is not None else de.CallResult(rows=[[None]])

    return body


def _fails(sqlstate, message="m"):
    def body(tables, call):
        return de.CallResult(error="x", sqlstate=sqlstate, message=message)

    return body


def _pair(rid, original: Routine | None, carried: Routine | None, depends_on=(), tables=("acc",),
          carried_text=None):  # fmt: skip
    src, tgt = f"CREATE {rid} source", carried_text or f"CREATE {rid} target"
    routines = {}
    if original is not None:
        routines[src] = original
    if carried is not None:
        routines[tgt] = carried
    compared = ComparedRoutine(_fn(rid, tables=tables), (de.SourceRoutine(rid, "function", src),),
                               (de.SourceRoutine(rid, "function", tgt),), depends_on)  # fmt: skip
    return compared, routines


def _factories(pairs, **kw):
    routines: dict[str, Routine] = {}
    for _, r in pairs:
        routines.update(r)
    opened: list[FakeTarget] = []

    def factory(which):
        def open_target():
            t = FakeTarget(routines=routines, **kw.get(which, {}))
            t.description = f"the fake {which}"
            opened.append(t)
            return t

        return open_target

    return factory("source"), factory("target"), opened


def _compare(pairs, **kw):
    source, target, opened = _factories(pairs, **{k: v for k, v in kw.items()
                                                  if k in ("source", "target")})  # fmt: skip
    rest = {k: v for k, v in kw.items() if k not in ("source", "target")}
    report = compare_routines([p for p, _ in pairs], TABLES, source=source, target=target,
                              dialect="postgresql", **rest)  # fmt: skip
    return report, opened


def test_an_equal_pair_has_no_differences_and_says_what_was_not_compared():
    pairs = [_pair("same_one", _returns(lambda n: n + 1), _returns(lambda n: n + 1)),
             _pair("changed", _returns(lambda n: n + 1), _returns(lambda n: n + 2))]  # fmt: skip
    report, opened = _compare(pairs)
    same, changed = report.results
    assert (same.outcome, same.status) == ("no_differences_found", "no_differences_found")
    assert same.coverage_limits and any("notices" in c for c in same.coverage_limits)
    n = same.calls_compared
    assert n > 1 and same.calls_succeeded == n
    assert same.reason == f"no differences found on {n} calls ({n} succeeded on both sides)"
    dumped = json.dumps(report.model_dump()).casefold()
    assert "proven" not in dumped and "equivalent" not in dumped
    assert (changed.outcome, changed.status) == ("different", "needs_attention")
    first = changed.differences[0]
    assert (first.aspect, first.call, first.source, first.target) == ("result", "baseline", 2, 3)
    assert report.counts == {"no_differences_found": 1, "different": 1, "could_not_compare": 0}
    assert all(t.closed for t in opened)


def test_a_routine_whose_every_call_fails_on_both_sides_could_not_be_compared():
    """Reproduction (review item 2): both bodies fail the same way on every call, whatever
    they would do otherwise."""
    report, _ = _compare([_pair("broken", _fails("22012"), _fails("22012"))])
    (r,) = report.results
    assert (r.outcome, r.reason_code, r.calls_succeeded) == ("could_not_compare",
                                                             "no_successful_call", 0)  # fmt: skip
    assert r.calls_compared > 0 and r.status == "not_compared"


def test_a_table_the_setup_left_empty_or_lacks_could_not_be_compared():
    """Round 3, item 5, and round 4, critical 1: a table with a non-plain name is created
    but left without rows, so an UPDATE or DELETE of it would change nothing on either
    side; a table the collection lacks is not created at all. Neither is compared."""
    pairs = [_pair("odd", _returns(lambda n: n), _returns(lambda n: n), tables=("odd name",)),
             _pair("gone", _returns(lambda n: n), _returns(lambda n: n), tables=("nosuch",))]  # fmt: skip
    odd, gone = _compare(pairs)[0].results
    assert (odd.outcome, odd.reason_code) == ("could_not_compare", "table_unseeded"), odd
    assert "without rows" in odd.reason
    assert (gone.outcome, gone.reason_code) == ("could_not_compare", "table_not_observed")
    assert "nosuch" in gone.reason


def test_an_error_with_no_sqlstate_could_not_be_compared():
    """Reproduction (review item 3): a harness error on both sides proves nothing."""

    def harness(tables, call):
        return de.CallResult(error="an enum argument needs its type to find a label")

    (r,) = _compare([_pair("h", harness, harness)])[0].results
    assert (r.outcome, r.reason_code) == ("could_not_compare", "harness_error")


def test_an_original_that_does_not_load_could_not_be_compared_and_the_others_still_are():
    pairs = [_pair("broken", None, _returns(lambda n: n)),
             _pair("fine", _returns(lambda n: n), _returns(lambda n: n))]  # fmt: skip
    broken, fine = _compare(pairs)[0].results
    assert (broken.outcome, broken.reason_code, broken.status) == (
        "could_not_compare", "original_does_not_load", "not_compared")  # fmt: skip
    assert "SQLSTATE 42601" in broken.reason and "the fake source" in broken.reason
    assert fine.outcome == "no_differences_found"


def test_a_carried_routine_that_does_not_load_is_different():
    (r,) = _compare([_pair("lost", _returns(lambda n: n), None)])[0].results
    assert (r.outcome, r.reason_code, r.status) == ("different", "carried_does_not_load",
                                                    "needs_attention")  # fmt: skip
    assert [(d.aspect, d.target) for d in r.differences] == [("load", "SQLSTATE 42601")]


def test_a_create_outside_the_targets_namespace_could_not_be_compared():
    pair = _pair("ns", _returns(lambda n: n), _returns(lambda n: n),
                 carried_text="CREATE app.ns target")  # fmt: skip
    (r,) = _compare([pair])[0].results
    assert (r.outcome, r.reason_code) == ("could_not_compare", "outside_namespace")
    assert "carried" in r.reason and "schema other than" in r.reason


def test_a_routine_whose_dependency_does_not_load_could_not_be_compared():
    pairs = [_pair("helper", _returns(lambda n: n), None),
             _pair("user", _returns(lambda n: n), _returns(lambda n: n), depends_on=("helper",))]  # fmt: skip
    user = _compare(pairs)[0].by_id()["user"]
    assert (user.outcome, user.reason_code) == ("could_not_compare", "dependency_not_compared")
    assert "helper" in user.reason and "target" in user.reason


def test_no_engine_for_the_source_could_not_compare_every_routine():
    def unavailable():
        raise ProofTargetUnavailable("no engine for postgresql 9.6: only 16 present")

    pairs = [_pair("a", _returns(lambda n: n), _returns(lambda n: n))]
    _, target, _ = _factories(pairs)
    report = compare_routines([p for p, _ in pairs], TABLES, source=unavailable, target=target,
                              dialect="postgresql")  # fmt: skip
    (r,) = report.results
    assert (r.outcome, r.reason_code) == ("could_not_compare", "source_engine_unavailable")
    assert "postgresql 9.6" in r.reason and r.status == "not_compared"


def test_a_routine_with_no_generated_call_could_not_be_compared():
    trigger_fn = RoutineProofInput(routine_id="tf", kind="function", name="tf", returns="trigger")
    compared = ComparedRoutine(trigger_fn, (de.SourceRoutine("tf", "function", "CREATE tf s"),),
                               (de.SourceRoutine("tf", "function", "CREATE tf t"),))  # fmt: skip
    noop = {"CREATE tf s": _returns(lambda n: n), "CREATE tf t": _returns(lambda n: n)}
    report = compare_routines([compared], TABLES,
                              source=lambda: FakeTarget(routines=noop),
                              target=lambda: FakeTarget(routines=noop), dialect="postgresql")  # fmt: skip
    (r,) = report.results
    assert (r.outcome, r.reason_code) == ("could_not_compare", "no_calls")
    assert "runs through its trigger" in r.reason


def test_a_routine_that_ends_the_transaction_could_not_be_compared():
    (r,) = _compare([_pair("c", _fails("2D000"), _fails("2D000"))])[0].results
    assert (r.outcome, r.reason_code) == ("could_not_compare", "transaction_control")


def _writes(tables, call):
    tables["acc"].append({"id": 100 + len(tables["acc"]), "bal": 0})
    return de.CallResult(rows=[[1]])


def test_a_commit_on_one_side_only_is_seen_on_the_call_it_happens():
    """Review item 8: the original commits and the carried routine does not; each call,
    the last one included, is a ``transaction`` difference, and the committed side is
    started again before the next call."""
    report, opened = _compare([_pair("w", _writes, _writes)], source={"commits": {"w"}})
    (r,) = report.results
    assert r.outcome == "different"
    calls = {d.call_index for d in r.differences if d.aspect == "transaction"}
    assert calls == set(range(1, r.calls_compared + 1))
    assert len([t for t in opened if t.description == "the fake source"]) == r.calls_compared + 1


def test_a_commit_on_both_sides_could_not_be_compared():
    report, _ = _compare([_pair("w", _writes, _writes)], source={"commits": {"w"}},
                         target={"commits": {"w"}})  # fmt: skip
    (r,) = report.results
    assert (r.outcome, r.reason_code) == ("could_not_compare", "commits")


def test_a_side_failing_mid_call_fails_that_routine_only():
    def boom(tables, call):
        raise ProofTargetError("the connection dropped")

    pairs = [_pair("bad", _returns(lambda n: n), boom),
             _pair("good", _returns(lambda n: n), _returns(lambda n: n))]  # fmt: skip
    bad, good = _compare(pairs)[0].results
    assert (bad.outcome, bad.reason_code) == ("could_not_compare", "engine_failed")
    assert "connection dropped" in bad.reason
    assert good.outcome == "no_differences_found"


def test_any_exception_fails_only_its_routine_or_batch():
    """Review item 15: an exception that is not the target's own never stops the other
    routines or batches."""

    def bug(tables, call):
        raise KeyError("not the target's")

    pairs = [_pair("bug", _returns(lambda n: n), bug),
             _pair("ok", _returns(lambda n: n), _returns(lambda n: n))]  # fmt: skip
    bug_result, ok = _compare(pairs)[0].results
    assert (bug_result.outcome, bug_result.reason_code) == ("could_not_compare", "engine_failed")
    assert ok.outcome == "no_differences_found"
    source, target, _ = _factories(pairs)
    tries = {"n": 0}

    def flaky():
        tries["n"] += 1
        if tries["n"] == 1:
            raise RuntimeError("anything")
        return source()

    report = compare_routines([p for p, _ in pairs], TABLES, source=flaky, target=target,
                              dialect="postgresql", batch_size=1)  # fmt: skip
    assert [r.reason_code for r in report.results] == ["engine_failed", None]


def test_batches_open_fresh_targets_and_one_failing_batch_never_stops_the_next():
    pairs = [_pair(f"r{i}", _returns(lambda n: n), _returns(lambda n: n)) for i in range(5)]
    seen: list[tuple[int, int]] = []
    source, target, opened = _factories(pairs)
    calls = {"n": 0}

    def flaky_source():
        calls["n"] += 1
        if calls["n"] == 2:
            raise ProofTargetError("did not start")
        return source()

    report = compare_routines([p for p, _ in pairs], TABLES, source=flaky_source, target=target,
                              dialect="postgresql", batch_size=2,
                              on_batch=lambda done, total: seen.append((done, total)))  # fmt: skip
    outcomes = [r.outcome for r in report.results]
    assert outcomes == [
        "no_differences_found",
        "no_differences_found",
        "could_not_compare",
        "could_not_compare",
        "no_differences_found",
    ]
    assert report.results[2].reason_code == "engine_failed"
    assert seen == [(1, 3), (2, 3), (3, 3)]
    assert all(t.closed for t in opened)


def test_two_engine_families_could_not_be_compared():
    pairs = [_pair("a", _returns(lambda n: n), _returns(lambda n: n))]
    source, _, _ = _factories(pairs)
    report = compare_routines([pairs[0][0]], TABLES, source=source,
                              target=lambda: FakeTarget("mysql", "8.0", routines=pairs[0][1]),
                              dialect="postgresql")  # fmt: skip
    assert report.results[0].reason_code == "engines_differ"


def test_what_a_side_could_not_freeze_is_in_the_reason():
    """Review item 17."""
    pairs = [_pair("x", _returns(lambda n: n), _returns(lambda n: n + 1))]
    report, _ = _compare(pairs, target={"notes": ("the server refused the random seed",)})
    assert report.results[0].reason.endswith("; note: the server refused the random seed")


def test_routines_of_a_batch_load_their_dependencies_from_another_batch():
    pairs = [_pair("helper", _returns(lambda n: n), _returns(lambda n: n)),
             _pair("user", _returns(lambda n: n), _returns(lambda n: n), depends_on=("helper",))]  # fmt: skip
    created: list[str] = []

    class Recording(FakeTarget):
        def create(self, routine):
            created.append(routine.name)
            return super().create(routine)

    routines = {**pairs[0][1], **pairs[1][1]}
    report = compare_routines([p for p, _ in pairs], TABLES,
                              source=lambda: Recording(routines=routines),
                              target=lambda: Recording(routines=routines),
                              dialect="postgresql", batch_size=1)  # fmt: skip
    assert [r.outcome for r in report.results] == ["no_differences_found", "no_differences_found"]
    assert created == ["helper", "helper", "helper", "user", "helper", "user"]


def test_differences_are_capped_and_counted():
    report, _ = _compare([_pair("x", _returns(lambda n: n), _returns(lambda n: n + 1))],
                         max_differences=2)  # fmt: skip
    (r,) = report.results
    assert len(r.differences) == 2 < r.differences_total


def test_bad_arguments():
    pairs = [_pair("a", _returns(lambda n: n), _returns(lambda n: n))]
    with pytest.raises(ValueError, match="unique"):
        _compare(pairs + pairs)
    with pytest.raises(ValueError, match="batch_size"):
        _compare(pairs, batch_size=0)


def test_no_routine_text_reaches_the_report_or_a_repr():
    pairs = [_pair("t", _returns(lambda n: n), None)]
    report, _ = _compare(pairs)
    dumped = json.dumps(report.model_dump()) + repr(report) + repr(pairs[0][0])
    assert "CREATE" not in dumped


def test_a_side_that_fails_while_it_loads_is_closed_and_never_used():
    pairs = [_pair("a", _returns(lambda n: n), _returns(lambda n: n)),
             _pair("b", _returns(lambda n: n), _returns(lambda n: n))]  # fmt: skip
    routines = {**pairs[0][1], **pairs[1][1]}
    opened: list[FakeTarget] = []

    class FailsToLoad(FakeTarget):
        def load_setup(self, setup):
            raise ProofTargetError("the rows did not load")

    def target():
        t = FailsToLoad(routines=routines)
        opened.append(t)
        return t

    report = compare_routines([p for p, _ in pairs], TABLES,
                              source=lambda: FakeTarget(routines=routines), target=target,
                              dialect="postgresql")  # fmt: skip
    assert [(r.outcome, r.reason_code) for r in report.results] == [
        ("could_not_compare", "engine_failed")] * 2  # fmt: skip
    assert opened and all(t.closed for t in opened)


def _half(fail_first: int):
    seen = [0]

    def body(tables, call):
        seen[0] += 1
        if seen[0] <= fail_first:
            return de.CallResult(error="x", sqlstate="P0001", message="m")
        return de.CallResult(rows=[[1]])

    return body


def test_a_routine_matched_mostly_on_errors_could_not_be_compared():
    """Round 2, item 1: one success and five matched errors prove nothing."""
    (r,) = _compare([_pair("m", _half(5), _half(5))])[0].results
    assert (r.outcome, r.reason_code) == ("could_not_compare", "mostly_errors"), r
    (ok,) = _compare([_pair("m", _half(1), _half(1))])[0].results
    assert ok.outcome == "no_differences_found", ok


def test_an_undefined_table_at_run_time_could_not_be_compared():
    """Round 2, item 1: a table the routine reads that its text did not show."""
    (r,) = _compare([_pair("u", _fails("42P01"), _fails("42P01"))])[0].results
    assert (r.outcome, r.reason_code) == ("could_not_compare", "table_not_observed")


def test_a_timeout_on_either_side_could_not_be_compared():
    """Round 2, item 2: a timeout says nothing about behaviour, on one side or both."""

    def slow(tables, call):
        return de.CallResult(error="the call ran past its time limit", timed_out=True)

    for original in (slow, _returns(lambda n: n)):
        (r,) = _compare([_pair("s", original, slow)])[0].results
        assert (r.outcome, r.reason_code) == ("could_not_compare", "timeout")


def test_a_routine_calling_a_function_that_cannot_be_frozen_could_not_be_compared():
    """Round 2, item 3."""
    pair = _pair("c", _returns(lambda n: n), _returns(lambda n: n),
                 carried_text="CREATE c target clock_timestamp()")  # fmt: skip
    (r,) = _compare([pair])[0].results
    assert (r.outcome, r.reason_code) == ("could_not_compare", "non_deterministic")
    assert "clock_timestamp" in r.reason


def test_the_guard_reads_hex_base64_json_keys_and_numeric_literals():
    """Round 2, suggestion: a ``bytea`` or base64 value of a literal, a JSON key, a string
    literal that looks like a number, and a string merely starting like a clock token."""
    text = ("CREATE FUNCTION f() RETURNS text LANGUAGE sql AS $$ "
            "SELECT 'Confidential discount code' || '4111111111111111' $$")  # fmt: skip
    guard = ValueGuard.for_texts([text], "postgresql", generated=set())
    secret = b"Confidential discount code"
    assert _w(guard.shown(secret.hex()))
    assert _w(guard.shown("\\x" + secret.hex()))
    import base64

    assert _w(guard.shown(base64.b64encode(secret).decode()))
    assert _w(guard.shown({"Confidential discount code": 1}))
    assert _w(guard.shown("4111111111111111"))
    assert _w(guard.shown("<now Confidential>"))
    assert guard.shown("<now+0us as dddd-dd-ddTdd:dd:dd+00:00>").startswith("<now")


def test_the_routine_body_is_code_not_one_literal():
    """Round 2, suggestion: a value that is a piece of the body's code is shown."""
    text = ("CREATE FUNCTION f(n int) RETURNS text LANGUAGE plpgsql AS $$ BEGIN "
            "RETURN 'tier-' || n; END $$")  # fmt: skip
    guard = ValueGuard.for_texts([text], "postgresql", generated=set())
    assert guard.shown("return") == "return" and guard.shown("BEGIN") == "BEGIN"
    assert _w(guard.shown("tier-3"))


# ------------------------------------------------------------- round 3 of the review


def test_values_are_compared_with_their_kind_and_their_columns_type():
    """Round 3, item 1: ``True`` and ``1``, ``1`` and ``1.0`` (inside JSON too), and two
    columns of one value but another engine type, all differ."""
    assert len(_diff(_run([[True]]), _run([[1]]))) == 1
    assert len(_diff(_run([[1]]), _run([[1.0]]))) == 1
    assert len(_diff(_run([[{"active": True}]]), _run([[{"active": 1}]]))) == 1
    assert _diff(_run([[{"a": 1, "b": 2}]]), _run([[{"b": 2, "a": 1}]])) == []  # jsonb content

    def typed(kind):
        return CallRun(de.CallResult(rows=[["6869"]], column_types=((kind,),)), {}, {}, T0)

    (d,) = _diff(typed("bytea"), typed("text"))
    assert (d.aspect, d.column, d.source, d.target) == ("result", "types", [["bytea"]], [["text"]])
    before = {"t": [{"id": 1, "v": True}]}
    a = _run(before=before, after={"t": [{"id": 1, "v": 1}]})
    found = _diff(_run(before=before), a, ["t"], keys={"t": ["id"]})
    assert found and all(d.table == "t" for d in found)  # a value that changed kind


def test_a_table_whose_column_types_differ_is_a_difference():
    a = CallRun(de.CallResult(), {}, {"made": []}, T0, types={"made": {"v": "numeric"}})
    b = CallRun(de.CallResult(), {}, {"made": []}, T0, types={"made": {"v": "text"}})
    (d,) = _diff(a, b, [])
    assert (d.table, d.column, d.source, d.target) == ("made", "types", {"v": "numeric"},
                                                       {"v": "text"})  # fmt: skip


def test_a_rollback_on_one_side_is_seen_and_on_both_could_not_be_compared():
    """Round 3, item 2: a ``ROLLBACK`` in the routine throws the caller's transaction
    away, which the sentinel row's absence in the call's own session shows."""
    report, _ = _compare([_pair("rb", _writes, _writes)], source={"rollbacks": {"rb"}})
    (r,) = report.results
    assert r.outcome == "different"
    assert {(d.source, d.target) for d in r.differences if d.aspect == "transaction"} == {
        ("rolled back", "open")}  # fmt: skip
    report, _ = _compare([_pair("rb", _writes, _writes)], source={"rollbacks": {"rb"}},
                         target={"rollbacks": {"rb"}})  # fmt: skip
    assert report.results[0].reason_code == "transaction_ended"


def test_exactly_half_the_calls_failing_is_compared_and_more_is_mostly_errors():
    """Round 3, item 9: with six calls, three failing on both sides is compared; four is
    "mostly errors"."""
    (half,) = _compare([_pair("m", _half(3), _half(3))])[0].results
    assert (half.calls_compared, half.outcome) == (6, "no_differences_found"), half
    (more,) = _compare([_pair("m", _half(4), _half(4))])[0].results
    assert (more.outcome, more.reason_code) == ("could_not_compare", "mostly_errors"), more


def test_a_routine_whose_statements_create_a_trigger_is_alone_in_its_batch():
    """Round 3, item 10."""
    proof = _proof("f")
    with_trigger = ComparedRoutine(proof, (de.SourceRoutine("f", "function", "x"),),
                                   (de.SourceRoutine("f", "function", "x"),
                                    de.SourceRoutine("t", "trigger", "x")))  # fmt: skip
    rs = [with_trigger, _plain(_proof("g")), _plain(_proof("h"))]
    assert _ids(plan_batches(rs, batch_size=10, dialect="postgresql")) == [["f"], ["g", "h"]]


def test_names_the_server_reads_another_way_are_refused():
    """Round 3, item 7: a PostgreSQL ``U&`` name (``U&"\\0061pp"`` is ``app``), a MySQL
    versioned comment, and a role change."""
    assert "U&" in create_refusal('CREATE FUNCTION U&"\\0061pp".f() RETURNS int LANGUAGE sql '
                                  "AS $$ SELECT 1 $$", "postgresql", "function", "public")  # fmt: skip
    assert "U&" in create_refusal("CREATE FUNCTION f() RETURNS void LANGUAGE plpgsql AS $$ "
                                  'BEGIN INSERT INTO U&"\\0061pp".accounts VALUES (1); END $$',
                                  "postgresql", "function", "public")  # fmt: skip
    assert "versioned" in create_refusal("CREATE PROCEDURE /*!50000 other.*/p() BEGIN END",
                                         "mysql", "procedure", "src")  # fmt: skip
    assert "role" in create_refusal("CREATE FUNCTION f() RETURNS void LANGUAGE plpgsql AS $$ "
                                    "BEGIN SET LOCAL ROLE x; END $$", "postgresql", "function",
                                    "public")  # fmt: skip


def test_dynamic_sql_and_more_unfrozen_functions_are_found():
    """Round 3, item 8."""
    from src.tools.routines.proof_screen import called_names, dynamic_sql

    def body(text):
        return [f"CREATE FUNCTION f() RETURNS void LANGUAGE plpgsql AS $$ BEGIN {text}; END $$"]

    assert dynamic_sql(body("EXECUTE 'select clock_' || 'timestamp()'"), "postgresql")
    assert dynamic_sql(body("EXECUTE format('select %s()', 'clock_timestamp')"), "postgresql")
    assert not dynamic_sql(body("EXECUTE 'select 1'"), "postgresql")
    assert not dynamic_sql(["CREATE TRIGGER t AFTER INSERT ON a FOR EACH ROW EXECUTE "
                            "FUNCTION f()"], "postgresql")  # fmt: skip
    assert dynamic_sql(["CREATE PROCEDURE p() BEGIN PREPARE st FROM @s; EXECUTE st; END"], "mysql")
    assert not dynamic_sql(["CREATE PROCEDURE p() BEGIN PREPARE st FROM 'SELECT 1'; "
                            "EXECUTE st; END"], "mysql")  # fmt: skip
    for fn in ("pg_postmaster_start_time", "txid_current_if_assigned", "pg_current_snapshot"):
        assert nondeterministic_calls(body(f"PERFORM {fn}()"), "postgresql", random_seeded=True)
    assert "clk_helper" in called_names(body("PERFORM clk_helper()")[0], "postgresql")


def test_a_batch_member_called_without_being_listed_is_screened_too():
    """Round 3, item 8: ``user`` calls ``helper`` (which reads ``clock_timestamp``) without
    listing it as a dependency; both are not compared."""
    helper = ComparedRoutine(_fn("helper"),
                             (de.SourceRoutine("helper", "function", "CREATE h s clock_timestamp()"),),
                             (de.SourceRoutine("helper", "function", "CREATE h t clock_timestamp()"),))  # fmt: skip
    user = ComparedRoutine(_fn("user"),
                           (de.SourceRoutine("user", "function", "CREATE u s helper(1)"),),
                           (de.SourceRoutine("user", "function", "CREATE u t helper(1)"),))  # fmt: skip
    routines = {"CREATE h s clock_timestamp()": _returns(lambda n: n),
                "CREATE h t clock_timestamp()": _returns(lambda n: n),
                "CREATE u s helper(1)": _returns(lambda n: n),
                "CREATE u t helper(1)": _returns(lambda n: n)}  # fmt: skip
    report = compare_routines([helper, user], TABLES,
                              source=lambda: FakeTarget(routines=routines),
                              target=lambda: FakeTarget(routines=routines), dialect="postgresql")  # fmt: skip
    assert [r.reason_code for r in report.results] == ["non_deterministic", "non_deterministic"]


def test_a_named_time_zone_without_zone_tables_could_not_be_compared():
    """Round 3, item 4."""
    text = "CREATE x CONVERT_TZ(NOW(), '+05:45', 'Europe/Paris')"
    pair = _pair("tz", _returns(lambda n: n), _returns(lambda n: n), carried_text=text)
    routines = dict(pair[1])

    class NoZones(FakeTarget):
        named_time_zones = False

    report = compare_routines([pair[0]], TABLES, source=lambda: NoZones("mysql", "8.0", routines=routines),
                              target=lambda: NoZones("mysql", "8.0", routines=routines),
                              dialect="mysql")  # fmt: skip
    assert report.results[0].reason_code == "named_time_zone"


def test_a_number_written_as_a_string_literal_is_withheld_and_withheld_values_are_numbered():
    """Round 3, suggestion 11."""
    text = ("CREATE FUNCTION f() RETURNS bigint LANGUAGE sql AS $$ "
            "SELECT '4111111111111111'::bigint $$")  # fmt: skip
    guard = ValueGuard.for_texts([text], "postgresql", generated=set())
    assert _w(guard.shown(4111111111111111))
    assert guard.shown(7) == 7
    other = ValueGuard.for_texts(["CREATE FUNCTION f() AS $$ SELECT 'alpha', 'beta' $$"],
                                 "postgresql", set())  # fmt: skip
    assert other.shown("alpha") != other.shown("beta") and other.shown("alpha") == other.shown(
        "alpha"
    )


# ------------------------------------------------------------- round 4 of the review


def test_a_routine_returning_a_cursor_could_not_be_compared():
    """Round 4, critical 2: only the cursor's name would be compared."""

    def cursor(tables, call):
        return de.CallResult(rows=[["<unnamed portal 1>"]], column_types=(("refcursor",),))

    (r,) = _compare([_pair("rc", cursor, cursor)])[0].results
    assert (r.outcome, r.reason_code) == ("could_not_compare", "refcursor")


def test_result_column_names_are_compared():
    """Round 4, critical 3: ``RETURNS TABLE(total ...)`` against ``(amount ...)``."""

    def named(name):
        return CallRun(de.CallResult(rows=[[1]], column_types=(("integer",),),
                                     column_names=((name,),)), {}, {}, T0)  # fmt: skip

    (d,) = _diff(named("total"), named("amount"))
    assert (d.aspect, d.column, d.source, d.target) == (
        "result",
        "names",
        [["total"]],
        [["amount"]],
    )
    assert _diff(named("total"), named("total")) == []
    guard = ValueGuard.for_texts(["CREATE PROCEDURE p() BEGIN SELECT 'secret column'; END"],
                                 "mysql", set())  # fmt: skip
    (d,) = _diff(named("secret column"), named("other"), guard=guard)
    assert _w(d.source[0][0])  # a column MySQL named after a literal is withheld


def test_session_state_and_counters_a_call_leaves_are_compared():
    """Round 4, important 1 and 2: a setting or user variable set on one side only, and a
    sequence drawn or set on one side only."""
    a = CallRun(de.CallResult(), {}, {}, T0, session={"DateStyle": "SQL, DMY"},
                counters={"audit_id_seq": "5"})  # fmt: skip
    b = CallRun(de.CallResult(), {}, {}, T0, session={}, counters={"audit_id_seq": "1000"})
    found = {(d.aspect, d.column, d.source, d.target) for d in _diff(a, b)}
    assert found == {("session", "DateStyle", "SQL, DMY", None),
                     ("sequence", "audit_id_seq", "5", "1000")}  # fmt: skip


def test_an_unread_session_is_listed_in_the_coverage_limits():
    pairs = [_pair("s", _returns(lambda n: n), _returns(lambda n: n))]
    (r,) = _compare(pairs)[0].results  # the fake reads no session state
    assert r.outcome == "no_differences_found"
    assert any("session settings" in c for c in r.coverage_limits)

    class Reads(FakeTarget):
        session = {}

    routines = pairs[0][1]
    (r,) = compare_routines([pairs[0][0]], TABLES, source=lambda: Reads(routines=routines),
                            target=lambda: Reads(routines=routines),
                            dialect="postgresql").results  # fmt: skip
    assert not any("session settings" in c for c in r.coverage_limits)


def test_a_postgresql_routine_reading_the_clock_lists_the_real_time_limit():
    """Round 4, important 3: PostgreSQL's clock is not frozen, so a date boundary is hit
    only by chance."""
    pair = _pair("d", _returns(lambda n: n), _returns(lambda n: n),
                 carried_text="CREATE d target current_date")  # fmt: skip
    (r,) = _compare([pair])[0].results
    assert any("real time" in c for c in r.coverage_limits), r.coverage_limits
    (plain,) = _compare([_pair("p", _returns(lambda n: n), _returns(lambda n: n))])[0].results
    assert not any("real time" in c for c in plain.coverage_limits)


def test_security_definer_is_a_listed_limit_and_set_config_has_its_own_reason():
    """Round 4, suggestions."""
    from src.tools.routines.comparison import COVERAGE_LIMITS

    assert any("SECURITY DEFINER" in c for c in COVERAGE_LIMITS)
    why = create_refusal("CREATE FUNCTION f() RETURNS text LANGUAGE sql AS $$ "
                         "SELECT set_config('datestyle', 'SQL', false) $$", "postgresql",
                         "function", "public")  # fmt: skip
    assert why == "it calls set_config, which may set search_path"


def test_an_unlisted_table_the_setup_left_empty_is_reported_as_unseeded():
    """Final verification, suggestion 2: the routine names ``odd name`` only in its text.
    The setup now creates every collection table a routine's text names; this one it
    creates without rows, so the reason is that it is unseeded, not that it is missing."""
    text = 'CREATE FUNCTION u() RETURNS void LANGUAGE plpgsql AS $$ BEGIN DELETE FROM "odd name"; END $$'
    user = ComparedRoutine(_fn("user", tables=()), (de.SourceRoutine("user", "function", text),),
                           (de.SourceRoutine("user", "function", text),))  # fmt: skip
    routines = {text: _returns(lambda n: n)}
    (r,) = compare_routines([user], TABLES, source=lambda: FakeTarget(routines=routines),
                            target=lambda: FakeTarget(routines=routines),
                            dialect="postgresql").results  # fmt: skip
    assert (r.outcome, r.reason_code) == ("could_not_compare", "table_unseeded"), r
