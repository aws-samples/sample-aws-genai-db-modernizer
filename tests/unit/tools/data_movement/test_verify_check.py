# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1 verify (#1998): the scripts' shape, the strict reading of the customer's output
files (untrusted input), the per-table result and its contract. No engine here: the real
engines run in ``test_verify_scripts_engine.py`` (PostgreSQL) and
``tests/integration/test_verify_scripts_mysql.py`` (MySQL)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.contracts.lineage_ops import UnsupportedOp
from src.contracts.transformation_contract import FieldRule
from src.contracts.verify_result import VerifyResult
from src.shared.contract_versioning import contract_for_artifact, current_version
from src.tools.data_movement.verify_check import (
    MAX_OUTPUT_BYTES,
    VerifyOutputError,
    compare,
    parse_output,
    read_verify,
)
from src.tools.data_movement.verify_scripts import (
    OUTPUTS,
    SCRIPTS,
    VerifyScriptError,
    render_script,
    verify_tables,
    write_verify_scripts,
)
from tests.unit.tools.data_movement.verify_fixtures import (
    SHA,
    contract,
    mysql_contract,
    pg_contract,
    rule,
)

SENTINEL = "s3cr3t-v4lue"
T1, T2 = "2026-11-09T10:00:00.000000", "2026-11-09T11:00:00.000000"
HEADER = f"modernizer-verify\t1\tsource\tpostgresql\t{SHA}\t{T1}"
END = "modernizer-verify-end\t\t\t\t\t"


def _file(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode()


def _parse(data: bytes, side: str = "source", engine: str = "postgresql") -> dict:
    out = parse_output(data, name="verify-source.tsv", side=side, engine=engine,  # type: ignore[arg-type]
                       contract_sha=SHA, positions={1, 2, 3})  # fmt: skip
    assert out.started == T1
    return out.rows


def test_a_well_formed_output_is_read_by_table_position():
    rows = _parse(_file(HEADER, "table\t2\t4\t0\t10\t20", "table\t1\t0\t0\t0\t0", END))
    assert rows == {2: (4, 0, 10, 20), 1: (0, 0, 0, 0)}
    # psql on Windows may end lines with CR LF.
    assert _parse(_file(HEADER + "\r", "table\t1\t1\t0\t2\t3\r", END + "\r")) == {1: (1, 0, 2, 3)}


@pytest.mark.parametrize(
    ("data", "problem"),
    [
        (b"", "empty"),
        (b"\xff\xfe", "not UTF-8"),
        (_file(HEADER, "table\t1\t1\t0\t2\t3"), "no end line"),
        (_file(HEADER, END, "table\t1\t1\t0\t2\t3"), "a line after the end line"),
        (_file(HEADER, f"table\t1\t1\t0\t2\t3\t{SENTINEL}", END), "7 tab-separated fields, not 6"),
        (_file(HEADER, f"table\t1\t1\t0\t2\t{SENTINEL}\x07", END), "control character"),
        (_file(HEADER, "table\t1\t1\t0\t2\t" + "9" * 200, END), "too long"),
        (_file(HEADER, f"table\t1\t1\t0\t2\t{SENTINEL}", END), "not a whole number"),
        (_file(HEADER, "table\t1\t01\t0\t2\t3", END), "not a whole number"),
        (_file(HEADER, "table\t1\t-1\t0\t2\t3", END), "not a whole number"),
        (_file(HEADER, "table\t9\t1\t0\t2\t3", END), "a table the script does not ask for"),
        (
            _file(HEADER, "table\t1\t1\t0\t2\t3", "table\t1\t1\t0\t2\t3", END),
            "the same table twice",
        ),
        (_file(HEADER, f"{SENTINEL}\t1\t1\t0\t2\t3", END), "not a table line"),
        (_file(HEADER, "modernizer-verify-end\t1\t\t\t\t", END), "the end line has values"),
        (_file(f"{SENTINEL}\t1\tsource\tpostgresql\t{SHA}\t{T1}", END), "not the header"),
        (_file(HEADER.replace(T1, SENTINEL), END), "not the run's start time"),
        (_file(HEADER.replace(SHA, "d" * 64), END), "another step contract"),
        (_file(HEADER.replace("source", "target"), END), "the other side's script"),
        (_file(HEADER.replace("postgresql", "mysql"), END), "another engine"),
    ],
)
def test_a_malformed_output_is_refused_whole_without_echoing_it(data, problem):
    with pytest.raises(VerifyOutputError) as exc:
        _parse(data)
    assert problem in str(exc.value)
    assert SENTINEL not in str(exc.value) and "d" * 64 not in str(exc.value)
    assert str(exc.value).startswith("verify-source.tsv: ")


def test_an_output_over_the_size_cap_is_refused_before_it_is_decoded():
    with pytest.raises(VerifyOutputError, match="over"):
        _parse(b"x" * (MAX_OUTPUT_BYTES + 1))


def _write(
    tmp_path: Path, c, side: str, run: int, lines: dict[int, tuple], started: str = ""
) -> None:
    """``lines``: (rows, a, b) or (rows, NULL-hash rows, a, b) per table position."""
    engine = ("postgresql" if side == "source" else "aurora_postgresql") if c.target.engine == \
        "aurora_postgresql" else ("mysql" if side == "source" else "aurora_mysql")  # fmt: skip
    body = [f"modernizer-verify\t1\t{side}\t{engine}\t{SHA}\t{started or (T1 if run == 1 else T2)}"]
    for n, v in lines.items():
        full = v if len(v) == 4 else (v[0], 0, v[1], v[2])
        body.append("table\t" + "\t".join(str(x) for x in (n, *full)))
    (tmp_path / OUTPUTS[(side, run)]).write_bytes(_file(*body, END))  # type: ignore[index]


def _read(tmp_path: Path, c=None):
    return read_verify(tmp_path, c or pg_contract(), job_id="job1", database_name="shop",
                       contract_sha=SHA)  # fmt: skip


def test_nothing_is_read_until_both_files_of_the_first_run_are_there(tmp_path):
    assert _read(tmp_path) is None
    _write(tmp_path, pg_contract(), "source", 1, {1: (1, 1, 1)})
    assert _read(tmp_path) is None


SAME = {1: (1, 1, 1), 2: (2, 2, 2), 3: (3, 3, 3)}


def test_one_file_of_the_second_run_without_the_other_is_refused(tmp_path):
    for side in ("source", "target"):
        _write(tmp_path, pg_contract(), side, 1, SAME)
    _write(tmp_path, pg_contract(), "source", 2, SAME)
    with pytest.raises(VerifyOutputError, match="the second run needs both files"):
        _read(tmp_path)


@pytest.mark.parametrize("stale", ["source", "target"])
def test_a_second_run_that_did_not_start_after_the_first_is_refused(tmp_path, stale):
    """The customer reran only the first pair: the old second pair never decides a result."""
    c = pg_contract()
    for side in ("source", "target"):
        _write(tmp_path, c, side, 1, SAME, started=T2)  # the first pair, rerun later
        _write(
            tmp_path,
            c,
            side,
            2,
            SAME,
            started=T1 if side == stale else "2026-11-09T12:00:00.000000",
        )
    with pytest.raises(VerifyOutputError) as exc:
        _read(tmp_path)
    assert str(exc.value).startswith(f"verify-{stale}-2.tsv: its run did not start after")


@pytest.mark.parametrize(
    ("target_started", "refused"),
    [
        ("2026-11-09T11:00:00.000001", True),
        ("2026-11-09T10:59:59.999999", False),
        ("2026-11-09T08:59:00.000000", True),
    ],
)
def test_a_pair_whose_sides_started_more_than_an_hour_apart_is_refused(
    tmp_path, target_started, refused
):
    """The customer reran only one side: its file is from another session."""
    c = pg_contract()
    _write(tmp_path, c, "source", 1, SAME, started=T1)
    _write(tmp_path, c, "target", 1, SAME, started=target_started)
    if refused:
        with pytest.raises(VerifyOutputError) as exc:
            _read(tmp_path)
        assert str(exc.value).startswith("verify-source.tsv and verify-target.tsv: ")
        assert "more than 1 hour apart" in str(exc.value) and "run both scripts again" in str(
            exc.value
        )
    else:
        assert _read(tmp_path).status == "match"


def test_a_second_pair_whose_sides_started_more_than_an_hour_apart_is_refused(tmp_path):
    c = pg_contract()
    _write(tmp_path, c, "source", 1, SAME, started=T1)
    _write(tmp_path, c, "target", 1, SAME, started=T1)
    _write(tmp_path, c, "source", 2, SAME, started=T2)
    _write(tmp_path, c, "target", 2, SAME, started="2026-11-09T12:30:00.000000")
    with pytest.raises(VerifyOutputError, match="verify-source-2.tsv and verify-target-2.tsv"):
        _read(tmp_path)


def _statuses(result) -> dict[str, str]:
    return {t.table_id: t.status for t in result.tables}


def test_each_table_is_match_differ_or_missing_after_one_run(tmp_path):
    # Positions follow the contract's sorted table ids: gone 1, nokey 2, things 3.
    _write(tmp_path, pg_contract(), "source", 1, {1: (1, 5, 5), 2: (4, 6, 6), 3: (3, 7, 7)})
    _write(tmp_path, pg_contract(), "target", 1, {2: (4, 6, 6), 3: (3, 7, 8)})
    result = _read(tmp_path)
    assert _statuses(result) == {"public.gone": "missing", "public.nokey": "match",
                                 "public.things": "differ"}  # fmt: skip
    gone = next(t for t in result.tables if t.table_id == "public.gone")
    assert (gone.missing_on, gone.source_rows, gone.target_rows) == (["target"], 1, None)
    assert result.status == "missing" and not result.second_run
    # One run: a difference cannot be told from a table still changing.
    assert "changing" not in _statuses(result).values()


def test_a_source_that_changed_between_the_two_runs_is_changing_not_differ(tmp_path):
    c = pg_contract()
    first = {1: (1, 1, 1), 2: (4, 6, 6), 3: (3, 7, 7)}
    _write(tmp_path, c, "source", 1, first)
    _write(tmp_path, c, "target", 1, {**first, 2: (3, 1, 1), 3: (3, 7, 9)})
    _write(tmp_path, c, "source", 2, {**first, 2: (5, 2, 2)})  # nokey changed on the source
    _write(tmp_path, c, "target", 2, {**first, 2: (4, 6, 6), 3: (3, 7, 9)})
    result = _read(tmp_path)
    assert result.second_run
    assert _statuses(result) == {"public.gone": "match", "public.nokey": "changing",
                                 "public.things": "differ"}  # fmt: skip
    assert result.status == "differ"
    nokey = next(t for t in result.tables if t.table_id == "public.nokey")
    assert (nokey.source_rows, nokey.target_rows) == (5, 4)  # the latest run's counts


def test_a_quiet_source_with_a_target_still_changing_is_changing(tmp_path):
    """DMS was still applying a backlog: Aurora moved between the runs, the source did not."""
    c = pg_contract()
    _write(tmp_path, c, "source", 1, SAME)
    _write(tmp_path, c, "target", 1, {**SAME, 3: (1, 9, 9)})
    _write(tmp_path, c, "source", 2, SAME)
    _write(tmp_path, c, "target", 2, {**SAME, 3: (2, 8, 8)})
    assert _statuses(_read(tmp_path))["public.things"] == "changing"


def test_a_null_hash_on_either_side_is_not_compared_never_match(tmp_path):
    c = pg_contract()
    _write(tmp_path, c, "source", 1, {**SAME, 3: (3, 1, 3, 3)})  # one row hashed NULL
    _write(tmp_path, c, "target", 1, {**SAME, 3: (3, 1, 3, 3)})  # the same sums all the same
    result = _read(tmp_path)
    things = next(t for t in result.tables if t.table_id == "public.things")
    assert (things.status, things.unhashed_on) == ("not_compared", ["source", "target"])
    assert result.status == "not_compared"


def test_the_latest_run_decides_a_match_even_after_a_first_run_difference(tmp_path):
    c = pg_contract()
    _write(tmp_path, c, "source", 1, SAME)
    _write(tmp_path, c, "target", 1, {**SAME, 3: (2, 2, 2)})  # Aurora was behind
    _write(tmp_path, c, "source", 2, SAME)
    _write(tmp_path, c, "target", 2, SAME)
    assert set(_statuses(_read(tmp_path)).values()) == {"match"}


def test_compare_needs_no_engine_for_its_rules():
    c = pg_contract()
    same = {1: (1, 0, 2, 3), 2: (4, 0, 5, 6), 3: (1, 0, 1, 1)}
    result = compare(c, {("source", 1): same, ("target", 1): {**same, 2: (4, 0, 0, 0)},
                         ("source", 2): {**same, 2: (5, 0, 5, 6)}, ("target", 2): same},
                     job_id="job1", database_name="shop", contract_sha=SHA)  # fmt: skip
    assert _statuses(result) == {"public.gone": "match", "public.nokey": "changing",
                                 "public.things": "match"}  # fmt: skip


def test_the_result_holds_no_checksum_and_loads_through_its_registered_contract(tmp_path):
    lines = {1: (1, 111111, 222222), 2: (2, 3, 4), 3: (3, 5, 6)}
    _write(tmp_path, pg_contract(), "source", 1, lines)
    _write(tmp_path, pg_contract(), "target", 1, lines)
    result = _read(tmp_path)
    dumped = result.model_dump_json()
    assert "111111" not in dumped and "222222" not in dumped
    assert contract_for_artifact("shop/job1/data-movement/step-1/verify.json") is VerifyResult
    assert result.contract_version == current_version(VerifyResult)
    assert result.gate_effect == "none"
    keyed = {t.table_id: t.dms_validated for t in result.tables}
    assert keyed == {"public.gone": True, "public.nokey": False, "public.things": True}


def test_the_contract_refuses_a_status_that_disagrees_or_changing_without_a_second_run():
    row = {"table_id": "public.t", "status": "changing", "dms_validated": True,
           "source_rows": 1, "target_rows": 1}  # fmt: skip
    base = {"job_id": "job1", "database_name": "shop", "source_engine": "postgresql",
            "target_engine": "aurora_postgresql", "contract_sha256": SHA,
            "checked_at": "2026-11-09T00:00:00Z", "tables": [row]}  # fmt: skip
    with pytest.raises(ValueError, match="changing needs a second run"):
        VerifyResult.model_validate({**base, "second_run": False, "status": "changing"})
    with pytest.raises(ValueError, match="disagrees"):
        VerifyResult.model_validate({**base, "second_run": True, "status": "match"})
    with pytest.raises(ValueError, match="missing exactly when"):
        VerifyResult.model_validate({**base, "second_run": True, "status": "missing",
                                     "tables": [{**row, "status": "missing"}]})  # fmt: skip
    with pytest.raises(ValueError, match="not_compared exactly when"):
        VerifyResult.model_validate({**base, "second_run": True, "status": "not_compared",
                                     "tables": [{**row, "status": "not_compared"}]})  # fmt: skip


# --------------------------------------------------------------------------- the scripts


@pytest.mark.parametrize("make", [pg_contract, mysql_contract])
def test_each_script_only_reads_in_read_only_transactions_and_prints_no_name(make):
    c = make()
    for side in ("source", "target"):
        text = render_script(c, SHA, side, target_database="shop_new")  # type: ignore[arg-type]
        statements = [s for s in text.splitlines() if s and not s.startswith(("--", "\\"))]
        assert not [s for s in statements if re.match(r"(?i)\s*(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|"
                                                       r"TRUNCATE|GRANT|CALL|DO|COPY)\b", s)]  # fmt: skip
        assert "READ ONLY" in text and ("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY" in text
                                        or "SET SESSION TRANSACTION READ ONLY" in text)  # fmt: skip
        tables = verify_tables(c)[0]
        assert text.count("READ ONLY;") >= len(tables)
        # A table line prints its position, never its name.
        for t in tables:
            assert f"SELECT 'table', '{t.n}'," in text


def test_the_mysql_target_reads_the_database_dms_writes_into():
    text = render_script(mysql_contract(), SHA, "target", target_database="shop_new")
    assert "FROM `shop_new`.`things`" in text and "FROM `shop`." not in text
    assert "FROM `shop`.`things`" in render_script(mysql_contract(), SHA, "source")


def test_a_name_with_a_control_character_is_left_out_never_written():
    c = contract("aurora_postgresql", [rule("public", "ok", [("id", "integer")]),
                                       rule("public", "bad\x07name", [("id", "integer")])])  # fmt: skip
    tables, left = verify_tables(c)
    assert [t.table_id for t in tables] == ["public.ok"] and left == [
        ("public.bad\x07name", "unsafe_name")
    ]
    for side in ("source", "target"):
        assert "bad" not in render_script(c, SHA, side)  # type: ignore[arg-type]


def test_an_unsupported_field_is_not_compared():
    r = rule("public", "t", [("id", "integer"), ("odd", "text")])
    odd = FieldRule(source_column="odd", target_column="odd", target_type="text",
                    op=UnsupportedOp(op="unsupported", description="no Aurora type"))  # fmt: skip
    r = r.model_copy(update={"fields": [r.fields[0], odd]})
    c = contract("aurora_postgresql", [r], unsupported_count=1)
    assert verify_tables(c)[0][0].columns == (("id", "id", "integer"),)


def test_scripts_are_refused_for_another_family_or_no_table(tmp_path):
    with pytest.raises(VerifyScriptError, match="the plan's source is mysql"):
        write_verify_scripts(pg_contract(), SHA, tmp_path, source_engine="mysql")
    with pytest.raises(VerifyScriptError, match="same family"):
        write_verify_scripts(contract("dynamodb", [rule("public", "t", [("id", "integer")])]),
                             SHA, tmp_path, source_engine="postgresql")  # fmt: skip
    with pytest.raises(VerifyScriptError, match="moves no table"):
        write_verify_scripts(contract("aurora_postgresql", []), SHA, tmp_path,
                             source_engine="postgresql")  # fmt: skip


def test_the_instructions_say_read_only_twice_full_scan_and_the_gate_is_unchanged(tmp_path):
    made = write_verify_scripts(mysql_contract(), SHA, tmp_path, source_engine="mysql",
                                target_database="shop_new")  # fmt: skip
    assert set(made.files) == {*SCRIPTS.values(), "verify.md"} and made.tables == 3
    text = (tmp_path / "verify.md").read_text()
    for phrase in ("does not change the gate", "START TRANSACTION READ ONLY", "full scan",
                   "never a value", "at most 1 hour apart", "--force", "The source again", "`changing`", "`differ`",
                   "`missing`", "`match`", "never connects to your databases"):  # fmt: skip
        assert phrase in text, phrase
    for name in OUTPUTS.values():
        assert str(tmp_path / name) in text
