# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Reading the customer's output of step 1's routine check script (#1947): untrusted
input, read strictly, compared with the contract's routines section."""

from __future__ import annotations

import pytest

from src.tools.data_movement import routine_check as rc
from src.tools.data_movement.routine_check import (
    RoutineCheckOutputError,
    check_routines,
    parse_check_output,
    read_check,
)
from tests.unit.tools.data_movement.routine_fixtures import (
    SENTINEL,
    contract_with_routines,
    dep_routine,
    migration,
    row,
)
from tests.unit.tools.sql._timing import assert_cost_ratio

SHA = "b" * 64
PG = "aurora_postgresql"
MY = "aurora_mysql"


def _pg_contract():
    return contract_with_routines(migration([
        row("public.audit()", signature=""),
        row("public.orders.trg", "trigger", name="trg", table="public.orders",
            dependencies=[dep_routine("function", "public.audit()")]),
        row("public.f(integer)", name="f", specific="f_1", signature="integer"),
        row("public.f(text)", name="f", specific="f_2", signature="text"),
        row("public.py_fn()", status="needs_attention", reasons=["postgresql-routine-language"]),
    ]))  # fmt: skip


def _output(engine, lines, sha=SHA, database="", end=True) -> bytes:
    header = ["modernizer-routines-check", "1", engine, sha, database, "", ""]
    body = ["\t".join(header), *("\t".join(line) for line in lines)]
    if end:
        body.append("\t".join(["modernizer-routines-check-end", "", "", "", "", "", ""]))
    return ("\n".join(body) + "\n").encode()


_PG_OK = [
    ["function", "public", "", "audit", "", "", ""],
    ["function", "public", "", "f", "integer", "", ""],
    ["function", "public", "", "f", "text", "", ""],
    ["trigger", "public", "orders", "trg", "", "", "D"],
]


def _result(contract, data, engine=PG, database=None):
    section = contract.routines
    rows = parse_check_output(
        data, section, engine=engine, contract_sha=SHA, target_database=database
    )
    return check_routines(section, rows, job_id="job1", database_name="shop", engine=engine,
                          contract_sha=SHA, target_database=database)  # fmt: skip


def _problems(result) -> dict[str, str]:
    return {m.routine_id: m.problem for m in result.mismatches}


def test_every_carried_routine_confirmed_still_fails_on_the_one_not_carried():
    result = _result(_pg_contract(), _output(PG, _PG_OK))
    assert (result.confirmed, result.carried, result.mismatches) == (4, 4, [])
    assert [(n.routine_id, n.reasons) for n in result.not_carried] == [
        ("public.py_fn()", ["postgresql-routine-language"])]  # fmt: skip
    assert (result.status, result.proof, result.label) == (
        "fail",
        "static",
        "checked, not load-tested",
    )


def test_a_routine_missing_on_the_target_is_named():
    result = _result(_pg_contract(), _output(PG, [_PG_OK[0], _PG_OK[2], _PG_OK[3]]))
    assert _problems(result) == {"public.f(integer)": "missing"}


def test_an_overload_with_other_argument_types_is_a_signature_mismatch():
    lines = [_PG_OK[0], ["function", "public", "", "f", "bigint", "", ""], _PG_OK[2], _PG_OK[3]]
    assert _problems(_result(_pg_contract(), _output(PG, lines))) == {
        "public.f(integer)": "signature_mismatch"}  # fmt: skip


def test_a_trigger_found_enabled_is_named():
    lines = [*_PG_OK[:3], ["trigger", "public", "orders", "trg", "", "", "O"]]
    assert _problems(_result(_pg_contract(), _output(PG, lines))) == {
        "public.orders.trg": "trigger_enabled"
    }


def _mysql_contract():
    return contract_with_routines(migration([
        row("shop.place_order", "procedure", schema="shop", status="rewritten",
            signature="IN INT(11), OUT VARCHAR(255) CHARSET utf8mb4"),
        row("shop.total", "function", schema="shop", signature="decimal(10, 2)", returns="DECIMAL(12,2)"),
        row("shop.orders_bi", "trigger", schema="shop", table="shop.orders"),
        row("shop.nightly", "event", schema="shop"),
    ], "mysql"))  # fmt: skip


_MY_OK = [
    ["procedure", "shop_new", "", "place_order", "IN int, OUT varchar(255)", "", ""],
    ["function", "shop_new", "", "total", "decimal(10,2)", "decimal(12,2)", ""],
]


def test_mysql_signatures_compare_modes_and_base_types_in_the_target_database():
    result = _result(_mysql_contract(), _output(MY, _MY_OK, database="shop_new"), MY, "shop_new")
    assert (result.status, result.confirmed, result.deferred) == ("pass", 2, 2)
    wrong = [
        ["procedure", "shop_new", "", "PLACE_ORDER", "OUT int, OUT varchar(255)", "", ""],
        _MY_OK[1],
    ]
    assert _problems(_result(_mysql_contract(), _output(MY, wrong, database="shop_new"), MY, "shop_new")) == {
        "shop.place_order": "signature_mismatch"}  # fmt: skip


def test_a_mysql_trigger_or_event_created_before_cutover_is_named():
    lines = [*_MY_OK, ["trigger", "shop_new", "orders", "orders_bi", "", "", ""],
             ["event", "shop_new", "", "nightly", "", "", "DISABLED"]]  # fmt: skip
    result = _result(_mysql_contract(), _output(MY, lines, database="shop_new"), MY, "shop_new")
    assert _problems(result) == {"shop.orders_bi": "present_before_cutover",
                                 "shop.nightly": "present_before_cutover"}  # fmt: skip


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "empty"),
        (b"\xff\xfe", "not UTF-8"),
        (b"SELECT 1\n", "1 tab-separated fields"),
        (_output(PG, _PG_OK, sha="c" * 64), "another step contract"),
        (_output(MY, []), "another Aurora engine"),
        (_output(PG, _PG_OK, end=False), "cut short"),
        (_output(PG, [*_PG_OK, _PG_OK[0]]), "twice"),
        (_output(PG, [["function", "public", "", "unknown_fn", "", "", ""]]), "does not ask for"),
        (_output(PG, [["view", "public", "", "audit", "", "", ""]]), "does not ask for"),
        (_output(PG, [["trigger", "public", "orders", "trg", "", "", "X"]]), "state"),
        (
            _output(PG, [["function", "public", "", "audit", "\x1b[2J", "", ""]]),
            "control character",
        ),
        (_output(PG, _PG_OK) + b"extra\tline\n", "after the end line"),
        (
            _output(PG, [["function", "public", "", "audit", f"'{SENTINEL}'", "", ""], ["x"]]),
            "line 3",
        ),
    ],
)
def test_a_malformed_output_is_refused_whole_without_echoing_it(data, message):
    with pytest.raises(RoutineCheckOutputError, match=message) as err:
        _result(_pg_contract(), data)
    assert SENTINEL not in str(err.value) and "unknown_fn" not in str(err.value)


def test_an_output_over_the_size_limit_is_refused(monkeypatch):
    monkeypatch.setattr(rc, "MAX_OUTPUT_BYTES", 10)
    with pytest.raises(RoutineCheckOutputError, match="over 10 bytes"):
        _result(_pg_contract(), _output(PG, _PG_OK))


def test_a_missing_output_file_reads_as_not_checked_yet(tmp_path):
    assert read_check(tmp_path / "none.tsv", _pg_contract(), job_id="job1", database_name="shop",
                      contract_sha=SHA, target_database=None) is None  # fmt: skip


def test_a_section_with_nothing_to_check_needs_no_file(tmp_path):
    contract = contract_with_routines(
        migration([row("public.a()", status="needs_attention", reasons=["x"])])
    )
    result = read_check(tmp_path / "none.tsv", contract, job_id="job1", database_name="shop",
                        contract_sha=SHA, target_database=None)  # fmt: skip
    assert (result.status, result.rows_read, [n.routine_id for n in result.not_carried]) == (
        "fail", 0, ["public.a()"])  # fmt: skip


def test_a_hundred_thousand_routines_are_read_and_compared_in_linear_time():
    def case(n: int):
        contract = contract_with_routines(migration(
            [row(f"public.f{i}(integer)", name=f"f{i}", signature="integer") for i in range(n)]))  # fmt: skip
        data = _output(
            PG, [["function", "public", "", f"f{i}", "integer", "", ""] for i in range(n)]
        )
        return contract, data

    large, reference = case(100_000), case(10_000)
    assert _result(*large).confirmed == 100_000
    assert_cost_ratio(lambda: _result(*large), lambda: _result(*reference), 25, max_rounds=3)
