# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Reading the customer's output of the cutover routine check script (#1948): untrusted
input, read strictly, compared with the routines the cutover acts on."""

from __future__ import annotations

import pytest

from src.contracts.cutover_check import CutoverCheckResult
from src.tools.data_movement.cutover_check import read_cutover_check
from src.tools.data_movement.routine_check import RoutineCheckOutputError
from tests.unit.tools.data_movement.routine_fixtures import (
    SENTINEL,
    contract_with_routines,
    dep_routine,
    migration,
    mysql_source,
    row,
)
from tests.unit.tools.data_movement.test_cutover_scripts import _mysql_case, _pg_case
from tests.unit.tools.data_movement.test_routine_scripts import SHA, _job
from tests.unit.tools.sql._timing import assert_cost_ratio

PG = "aurora_postgresql"
MY = "aurora_mysql"


def _output(
    engine, lines, sha=SHA, database="", magic="modernizer-cutover-check", scheduler=None
) -> bytes:
    header = [magic, "1", engine, sha, database, "", ""]
    if scheduler is not None:
        lines = [*lines, ["event_scheduler", "", "", "", "", "", scheduler]]
    end = [f"{magic}-end", "", "", "", "", "", ""]
    return ("\n".join("\t".join(x) for x in [header, *lines, end]) + "\n").encode()


def _read(
    tmp_path, store, contract, data: bytes | None, database=None
) -> CutoverCheckResult | None:
    path = tmp_path / "cutover-check.tsv"
    if data is not None:
        path.write_bytes(data)
    return read_cutover_check(path, contract, store, job_id="job1", database_name="shop",
                              contract_sha=SHA, target_database=database)  # fmt: skip


def _problems(result) -> dict[str, str]:
    return {m.routine_id: m.problem for m in result.mismatches}


def test_postgresql_triggers_pass_only_when_enabled(tmp_path):
    store, contract = _pg_case(tmp_path)
    ok = [["trigger", "public", "orders", "trg_a", "", "", "O"],
          ["trigger", "public", "orders", "trg_b", "", "", "O"]]  # fmt: skip
    result = _read(tmp_path, store, contract, _output(PG, ok))
    assert (result.status, result.expected, result.confirmed, result.rows_read) == ("pass", 2, 2, 2)
    still_off = [ok[0], ["trigger", "public", "orders", "trg_b", "", "", "D"]]
    assert _problems(_read(tmp_path, store, contract, _output(PG, still_off))) == {
        "public.orders.trg_b": "trigger_state_differs"}  # fmt: skip
    replica_only = [ok[0], ["trigger", "public", "orders", "trg_b", "", "", "R"]]
    assert _problems(_read(tmp_path, store, contract, _output(PG, replica_only))) == {
        "public.orders.trg_b": "trigger_state_differs"}  # fmt: skip
    gone = _read(tmp_path, store, contract, _output(PG, ok[:1]))
    assert (gone.status, _problems(gone)) == ("fail", {"public.orders.trg_b": "missing"})


def test_no_output_yet_is_none_and_nothing_to_act_on_needs_no_file(tmp_path):
    store, contract = _pg_case(tmp_path)
    assert _read(tmp_path, store, contract, None) is None
    store, contract = _pg_case(tmp_path / "none", triggers=())
    result = _read(tmp_path / "none", store, contract, None)
    assert (result.status, result.expected, result.rows_read) == ("pass", 0, 0)


def test_a_trigger_step_1_does_not_carry_fails_the_check_named_with_its_reasons(tmp_path):
    extra = [
        row("public.py_fn()", specific="py_2", status="needs_attention",
            reasons=["postgresql-routine-language"]),
        row("public.orders.trg_py", "trigger", name="trg_py", table="public.orders",
            specific="orders.trg_py", dependencies=[dep_routine("function", "public.py_fn()")]),
    ]  # fmt: skip
    store, contract = _pg_case(tmp_path, triggers=("trg_a",), extra_rows=extra)
    result = _read(tmp_path, store, contract,
                   _output(PG, [["trigger", "public", "orders", "trg_a", "", "", "O"]]))  # fmt: skip
    assert (result.status, result.confirmed, result.mismatches) == ("fail", 1, [])
    assert [(n.routine_id, n.reasons) for n in result.not_carried] == [
        ("public.orders.trg_py", ["depends_on_not_carried"])]  # fmt: skip


def test_mysql_events_must_have_the_status_their_source_create_gives(tmp_path):
    store, contract = _mysql_case(tmp_path, {"nightly": "ENABLE", "paused": "DISABLE",
                                             "replica": "DISABLE ON SLAVE"})  # fmt: skip
    ok = [["trigger", "shop_new", "orders", "orders_bi", "", "", ""],
          ["event", "shop_new", "", "nightly", "", "", "ENABLED"],
          ["event", "shop_new", "", "paused", "", "", "DISABLED"],
          ["event", "shop_new", "", "replica", "", "", "SLAVESIDE_DISABLED"]]  # fmt: skip
    def out(lines, scheduler="ON"):
        return _output(MY, lines, database="shop_new", scheduler=scheduler)

    result = _read(tmp_path, store, contract, out(ok), "shop_new")
    assert (result.status, result.expected, result.confirmed) == ("pass", 4, 4)
    assert (result.events_scheduled, result.event_scheduler) == (1, "ON")
    newer = [*ok[:3], ["event", "shop_new", "", "replica", "", "", "REPLICA_SIDE_DISABLED"]]
    assert _read(tmp_path, store, contract, out(newer), "shop_new").status == "pass"
    drift = [ok[0], ["event", "shop_new", "", "nightly", "", "", "DISABLED"],
             ["event", "shop_new", "", "paused", "", "", "ENABLED"]]  # fmt: skip
    assert _problems(_read(tmp_path, store, contract, out(drift), "shop_new")) == {
        "shop.nightly": "event_status_differs", "shop.paused": "event_status_differs",
        "shop.replica": "missing"}  # fmt: skip


@pytest.mark.parametrize("value", ["OFF", "DISABLED"])
def test_an_enabled_event_with_the_scheduler_off_fails_named(tmp_path, value):
    store, contract = _mysql_case(tmp_path, {"nightly": "ENABLE"})
    ok = [["trigger", "shop_new", "orders", "orders_bi", "", "", ""],
          ["event", "shop_new", "", "nightly", "", "", "ENABLED"]]  # fmt: skip
    result = _read(tmp_path, store, contract,
                   _output(MY, ok, database="shop_new", scheduler=value), "shop_new")  # fmt: skip
    assert (result.status, result.mismatches, result.event_scheduler) == ("fail", [], value)
    assert result.scheduler_off


def test_only_disabled_events_need_no_scheduler_but_the_line_must_be_there(tmp_path):
    store, contract = _mysql_case(tmp_path, {"paused": "DISABLE"})
    ok = [["trigger", "shop_new", "orders", "orders_bi", "", "", ""],
          ["event", "shop_new", "", "paused", "", "", "DISABLED"]]  # fmt: skip
    result = _read(tmp_path, store, contract,
                   _output(MY, ok, database="shop_new", scheduler="OFF"), "shop_new")  # fmt: skip
    assert (result.status, result.events_scheduled) == ("pass", 0)
    with pytest.raises(RoutineCheckOutputError, match="no event_scheduler line"):
        _read(tmp_path, store, contract, _output(MY, ok, database="shop_new"), "shop_new")
    with pytest.raises(RoutineCheckOutputError, match="setting value"):
        _read(tmp_path, store, contract,
              _output(MY, ok, database="shop_new", scheduler="MAYBE"), "shop_new")  # fmt: skip


def test_triggers_left_out_for_their_order_or_state_fail_the_check_named(tmp_path):
    from tests.unit.tools.data_movement.test_cutover_scripts import _two_triggers

    store, contract = _mysql_case(tmp_path, **_two_triggers(orders=(1, None)))
    data = _output(
        MY, [["trigger", "shop_new", "orders", "orders_bi", "", "", ""]], database="shop_new"
    )
    result = _read(tmp_path, store, contract, data, "shop_new")
    assert (result.status, result.confirmed, result.mismatches) == ("fail", 1, [])
    assert {(m.routine_id, m.reason) for m in result.left_out} == {
        ("shop.first_bi", "trigger_order_unknown"), ("shop.second_bi", "trigger_order_unknown")}  # fmt: skip
    store, contract = _pg_case(tmp_path / "pg", states={"public.orders.trg_b": None})
    ok = [["trigger", "public", "orders", "trg_a", "", "", "O"]]
    result = _read(tmp_path / "pg", store, contract, _output(PG, ok))
    assert [(m.routine_id, m.reason) for m in result.left_out] == [
        ("public.orders.trg_b", "trigger_state_unknown")]  # fmt: skip


def test_each_postgresql_source_state_is_checked_exactly(tmp_path):
    names = ("t_o", "t_a", "t_r", "t_d")
    from tests.unit.tools.data_movement.test_cutover_scripts import _pg_case as case

    store, contract = case(tmp_path, triggers=names,
                           states={f"public.orders.{n}": n[-1].upper() for n in names})  # fmt: skip
    ok = [["trigger", "public", "orders", n, "", "", n[-1].upper()] for n in names]
    assert _read(tmp_path, store, contract, _output(PG, ok)).status == "pass"
    swapped = [*ok[:2], ["trigger", "public", "orders", "t_r", "", "", "O"],
               ["trigger", "public", "orders", "t_d", "", "", "O"]]  # fmt: skip
    assert _problems(_read(tmp_path, store, contract, _output(PG, swapped))) == {
        "public.orders.t_r": "trigger_state_differs", "public.orders.t_d": "trigger_state_differs"}  # fmt: skip


def test_a_state_given_only_as_enabled_or_disabled_is_checked_as_o_or_d(tmp_path):
    states = {"public.orders.trg_a": "enabled", "public.orders.trg_b": "disabled"}
    store, contract = _pg_case(tmp_path, states=states)
    ok = [["trigger", "public", "orders", "trg_a", "", "", "O"],
          ["trigger", "public", "orders", "trg_b", "", "", "D"]]  # fmt: skip
    assert _read(tmp_path, store, contract, _output(PG, ok)).status == "pass"
    always = [["trigger", "public", "orders", "trg_a", "", "", "A"], ok[1]]
    assert _problems(_read(tmp_path, store, contract, _output(PG, always))) == {
        "public.orders.trg_a": "trigger_state_differs"}  # fmt: skip


def test_mysql_names_are_matched_case_insensitively_in_the_target_database(tmp_path):
    store, contract = _mysql_case(tmp_path)
    data = _output(MY, [["trigger", "SHOP_NEW", "orders", "ORDERS_BI", "", "", ""]],
                   database="shop_new")  # fmt: skip
    assert _read(tmp_path, store, contract, data, "shop_new").status == "pass"


@pytest.mark.parametrize(
    ("data", "words"),
    [
        # step 1's routine check output is not the cutover check's, nor the other way round
        (
            _output(PG, [], magic="modernizer-routines-check"),
            "line 1 is not the header cutover-check.sql",
        ),
        (_output(MY, []), "another Aurora engine"),
        (_output(PG, [], sha="c" * 64), "run run_dms_step.py cutover-scripts"),
        (
            _output(PG, [["trigger", "public", "orders", f"x{SENTINEL}", "", "", "O"]]),
            "does not ask for",
        ),
        (
            _output(PG, [["trigger", "public", "orders", "trg_a", "", "", "Z"]]),
            "a state the engine",
        ),
        (_output(PG, [["function", "public", "", "audit", "", "", ""]]), "does not ask for"),
        (_output(PG, []).split(b"\n")[0] + b"\n", "cut short"),
        (b"\xff\xfe", "not UTF-8"),
    ],
)
def test_a_malformed_or_foreign_output_is_refused_whole_without_echoing_it(tmp_path, data, words):
    store, contract = _pg_case(tmp_path)
    with pytest.raises(RoutineCheckOutputError) as err:
        _read(tmp_path, store, contract, data)
    assert words in str(err.value) and SENTINEL not in str(err.value)


def test_the_result_holds_no_routine_text(tmp_path):
    store, contract = _mysql_case(tmp_path, {"nightly": "ENABLE"})
    result = _read(tmp_path, store, contract,
                   _output(MY, [], database="shop_new", scheduler="ON"), "shop_new")  # fmt: skip
    assert SENTINEL not in result.model_dump_json()
    assert _problems(result) == {"shop.orders_bi": "missing", "shop.nightly": "missing"}


def test_an_event_whose_source_text_is_gone_is_checked_for_existence_only(tmp_path):
    artifact = migration([row("shop.e", "event", schema="shop", specific="e", status="rewritten")],
                         "mysql")  # fmt: skip
    store = _job(tmp_path, artifact, [mysql_source("other", "CREATE PROCEDURE `other`() SELECT 1")],
                 "mysql")  # fmt: skip
    contract = contract_with_routines(artifact)
    data = _output(MY, [["event", "shop", "", "e", "", "", "DISABLED"]], database="shop",
                   scheduler="OFF")  # fmt: skip
    assert _read(tmp_path, store, contract, data, "shop").status == "pass"


def test_a_hundred_thousand_trigger_lines_read_in_linear_time(tmp_path):
    def case(n: int):
        folder = tmp_path / str(n)
        folder.mkdir()
        store, contract = _pg_case(folder, triggers=[f"t{i}" for i in range(n)])
        data = _output(
            PG, [["trigger", "public", "orders", f"t{i}", "", "", "O"] for i in range(n)]
        )
        (folder / "cutover-check.tsv").write_bytes(data)
        return folder, store, contract

    def read(folder, store, contract):
        return read_cutover_check(folder / "cutover-check.tsv", contract, store, job_id="job1",
                                  database_name="shop", contract_sha=SHA, target_database=None)  # fmt: skip

    big, ref = case(100_000), case(10_000)
    assert read(*big).confirmed == 100_000
    assert_cost_ratio(lambda: read(*big), lambda: read(*ref), 25, max_rounds=3)
