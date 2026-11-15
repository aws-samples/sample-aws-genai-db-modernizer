# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's view scripts and the reading of their check output (#2082)."""

from __future__ import annotations

import json
import re

import pytest

from src.contracts.view_check import ViewCheckResult
from src.shared.cutover_message import CUTOVER_DISCLAIMER
from src.tools.data_movement.routine_check import RoutineCheckOutputError
from src.tools.data_movement.view_check import check_views, parse_view_output, read_view_check
from src.tools.data_movement.view_scripts import (
    CHECK_FILE,
    CHECK_OUTPUT,
    DEPLOY_FILE,
    INSTRUCTIONS_FILE,
    VIEWS_CHECK,
)
from tests.unit.tools.data_movement.view_fixtures import (
    SENTINEL,
    SHA,
    contract_with_views,
    job,
    mysql_view_source,
    pg_view_source,
    view,
    write,
)

_PG = 'CREATE OR REPLACE VIEW "public"."{name}" ("id") AS\n SELECT o.id FROM orders o{tail};'
_MY = "CREATE OR REPLACE SQL SECURITY DEFINER VIEW `shop`.`{name}` (`id`) AS\nselect 1 AS `id`;"


def _pg_case(tmp_path, sources=None, rows=None):
    rows = rows or [view("top", views=["base"]), view("base")]
    contract = contract_with_views(rows)
    if sources is None:
        sources = [pg_view_source("top", _PG.format(name="top", tail=f" WHERE o.note = '{SENTINEL}'")),
                   pg_view_source("base", _PG.format(name="base", tail=""))]  # fmt: skip
    store = job(tmp_path, sources)
    return store, contract


def test_the_postgresql_deploy_creates_the_views_in_dependency_order_in_one_transaction(tmp_path):
    store, contract = _pg_case(tmp_path)
    scripts = write(tmp_path, store, contract)
    assert (scripts.status, scripts.carried, scripts.in_script, scripts.refused) == (
        "ready",
        2,
        2,
        [],
    )
    text = (tmp_path / "step" / DEPLOY_FILE).read_text()
    assert text.index('"public"."base"') < text.index('"public"."top"')
    assert text.index("BEGIN;") < text.index("SET LOCAL lock_timeout = '500ms';") < text.index(
        "-- 1/2 view base") < text.rindex("COMMIT;")  # fmt: skip
    assert 'SET LOCAL search_path TO "public";' in text
    assert "before routines-deploy.sql" in text
    assert text.splitlines()[0] == f"-- {CUTOVER_DISCLAIMER}"
    for word in ("caught up", "in sync", "proven", "ready"):
        assert word not in text.lower().replace("already", "")


def test_writing_the_scripts_twice_gives_the_same_bytes(tmp_path):
    store, contract = _pg_case(tmp_path)
    write(tmp_path, store, contract)
    first = {
        n: (tmp_path / "step" / n).read_bytes()
        for n in (DEPLOY_FILE, CHECK_FILE, INSTRUCTIONS_FILE)
    }
    write(tmp_path, store, contract)
    assert first == {n: (tmp_path / "step" / n).read_bytes() for n in first}
    assert all("CREATE OR REPLACE VIEW" in b.decode() for n, b in first.items() if n == DEPLOY_FILE)


def test_a_view_text_and_its_literals_reach_only_the_deploy_script(tmp_path):
    store, contract = _pg_case(tmp_path)
    scripts = write(tmp_path, store, contract)
    step = tmp_path / "step"
    assert SENTINEL in (step / DEPLOY_FILE).read_text()
    for name in (CHECK_FILE, INSTRUCTIONS_FILE):
        assert SENTINEL not in (step / name).read_text()
    assert SENTINEL not in json.dumps(scripts.__dict__)
    assert SENTINEL not in contract.model_dump_json()


def test_a_mysql_view_runs_under_its_collation_with_no_definer_or_algorithm(tmp_path):
    contract = contract_with_views([view("v", engine="aurora_mysql")], "aurora_mysql")
    text_in = _MY.format(name="v").replace("1 AS", f"'{SENTINEL}' AS")
    store = job(tmp_path, [mysql_view_source("v", text_in)], "mysql")
    assert write(tmp_path, store, contract).in_script == 1
    text = (tmp_path / "step" / DEPLOY_FILE).read_text()
    assert (
        "SET NAMES utf8mb4;\nSET SESSION collation_connection = 'utf8mb4_0900_ai_ci';\nCREATE OR "
        in text
    )
    assert "DEFINER is left out" in text and "ALGORITHM is not in the collection" in text
    assert "BEGIN;" not in text and SENTINEL in text


@pytest.mark.parametrize(
    "source,code",
    [
        (None, "source_missing"),
        ("not exact", "source_text_not_exact"),
        ("CREATE TABLE `shop`.`v` (id int);", "not_a_view_statement"),
        ("collation", "session_not_allowed"),
        ("delimiter", "delimiter_in_text"),
    ],
)
def test_a_view_the_script_cannot_hold_is_refused_by_code_and_so_is_its_reader(
    tmp_path, source, code
):
    rows = [view("v", engine="aurora_mysql"), view("w", engine="aurora_mysql", views=["v"])]
    contract = contract_with_views(rows, "aurora_mysql")
    good = mysql_view_source("w", _MY.format(name="w"))
    bad = None
    if source == "not exact":
        bad = mysql_view_source("v", _MY.format(name="v")).model_copy(update={"exact": False})
    elif source == "collation":
        bad = mysql_view_source("v", _MY.format(name="v"), collation="x'; DROP")
    elif source == "delimiter":
        bad = mysql_view_source(
            "v", _MY.format(name="v").replace("select 1", "select 1\ndelimiter //")
        )
    elif source is not None:
        bad = mysql_view_source("v", source)
    store = job(tmp_path, [good, *([bad] if bad else [])], "mysql")
    scripts = write(tmp_path, store, contract)
    assert scripts.refused == [("v", code), ("w", "depends_on_refused")]
    assert scripts.in_script == 0
    assert "`shop`.`w`" not in (tmp_path / "step" / DEPLOY_FILE).read_text()


def test_a_mysql_view_with_another_target_database_is_refused(tmp_path):
    contract = contract_with_views([view("v", engine="aurora_mysql")], "aurora_mysql")
    store = job(tmp_path, [mysql_view_source("v", _MY.format(name="v"))], "mysql")
    assert write(tmp_path, store, contract, target_database="shop_new").refused == [
        ("v", "target_database_differs")]  # fmt: skip
    assert write(tmp_path, store, contract, target_database="shop").refused == []


def test_hostile_names_are_refused_or_quoted_never_written_raw(tmp_path):
    rows = [view("v\nDROP", schema="public"), view("q'\"`x", columns=[("c'\"", "text")])]
    contract = contract_with_views(rows)
    quoted = 'CREATE OR REPLACE VIEW "public"."q\'""`x" ("c\'""") AS SELECT 1;'
    store = job(tmp_path, [pg_view_source("q'\"`x", quoted)])
    scripts = write(tmp_path, store, contract)
    assert scripts.refused == [("v\nDROP", "unsafe_name")]
    step = tmp_path / "step"
    check = (step / CHECK_FILE).read_text()
    assert "('public', 'q''\"`x')" in check  # a quoted literal, the quote doubled
    assert "DROP" not in check and "\nDROP" not in (step / DEPLOY_FILE).read_text()
    deploy = (step / DEPLOY_FILE).read_text()
    assert "-- 2/2 view q'\"`x" in deploy or "-- 1/2 view q'\"`x" in deploy


def test_the_scripts_create_the_view_schema_step_1_does_not_and_grant_nothing(tmp_path):
    contract = contract_with_views([view("v", schema="reports", tables=["public.orders"])])
    text = 'CREATE OR REPLACE VIEW "reports"."v" ("id") AS SELECT 1;'
    store = job(tmp_path, [pg_view_source("v", text, schema="reports")])
    write(tmp_path, store, contract)
    deploy = (tmp_path / "step" / DEPLOY_FILE).read_text()
    assert 'CREATE SCHEMA IF NOT EXISTS "reports";' in deploy
    assert deploy.index("CREATE SCHEMA IF") < deploy.index('CREATE OR REPLACE VIEW "reports"')
    assert "SELECT on its views, for the application user is yours to add" in deploy
    assert "CREATE SCHEMA" not in deploy.replace('CREATE SCHEMA IF NOT EXISTS "reports"', "")
    assert "GRANT" not in deploy
    _, plain = _pg_case(tmp_path / "plain")
    write(tmp_path / "plain", job(tmp_path / "plain", None), plain)
    assert "CREATE SCHEMA" not in (tmp_path / "plain" / "step" / DEPLOY_FILE).read_text()


def test_nothing_to_deploy_removes_an_earlier_runs_scripts(tmp_path):
    store, contract = _pg_case(tmp_path)
    write(tmp_path, store, contract)
    none = contract_with_views([view("v", tables=["public.gone"])])
    assert write(tmp_path, store, none).status == "nothing_to_deploy"
    assert not any(
        (tmp_path / "step" / n).exists() for n in (DEPLOY_FILE, CHECK_FILE, INSTRUCTIONS_FILE)
    )


def test_the_check_script_is_read_only_and_asks_for_the_carried_views_only(tmp_path):
    store, contract = _pg_case(tmp_path, rows=[view("top"), view("far", tables=["public.gone"])])
    write(tmp_path, store, contract)
    text = (tmp_path / "step" / CHECK_FILE).read_text()
    assert "BEGIN TRANSACTION READ ONLY;" in text and text.rstrip().endswith("COMMIT;")
    assert "('public', 'top')" in text and "far" not in text
    for verb in ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "GRANT"):
        assert not re.search(rf"\b{verb}\b", text.upper())
    my = contract_with_views([view("v", engine="aurora_mysql")], "aurora_mysql")
    write(
        tmp_path / "my",
        job(tmp_path / "my", [mysql_view_source("v", _MY.format(name="v"))], "mysql"),
        my,
    )
    assert "START TRANSACTION READ ONLY;" in (tmp_path / "my" / "step" / CHECK_FILE).read_text()


# --------------------------------------------------------------------------- the check output


def _out(engine: str, lines: list[list[str]], *, sha: str = SHA, end: bool = True) -> bytes:
    header = [VIEWS_CHECK.magic, "1", engine, sha, "", "", ""]
    rows = [header, *lines] + ([[VIEWS_CHECK.end, "", "", "", "", "", ""]] if end else [])
    return ("\n".join("\t".join(r) for r in rows) + "\n").encode()


def _pg_contract():
    return contract_with_views([view("top", views=["base"], columns=[("id", "bigint"), ("note", "text")]),
                                view("base"), view("far", tables=["public.gone"])])  # fmt: skip


def _check(contract, data, engine="aurora_postgresql"):
    rows = parse_view_output(data, contract.views_section, engine=engine, contract_sha=SHA,
                             target_database=None)  # fmt: skip
    return check_views(contract.views_section, rows, job_id="job1", database_name="shop",
                       engine=engine, contract_sha=SHA)  # fmt: skip


def test_the_check_confirms_each_carried_view_and_lists_the_rest():
    contract = _pg_contract()
    good = [["view", "public", "base", "1", "id", "bigint", "v"],
            ["view", "public", "top", "1", "id", "bigint", "v"],
            ["view", "public", "top", "2", "note", "text", "v"]]  # fmt: skip
    result = _check(contract, _out("aurora_postgresql", good))
    assert (result.status, result.carried, result.confirmed, result.mismatches) == (
        "pass",
        2,
        2,
        [],
    )
    assert [(n.view_id, n.reasons) for n in result.not_carried] == [
        ("far", ["depends_on_table_not_in_step"])]  # fmt: skip
    ViewCheckResult.model_validate(result.model_dump(mode="json"))


@pytest.mark.parametrize(
    "lines,problem,columns",
    [
        ([], "missing", []),
        ([["view", "public", "top", "1", "id", "bigint", "r"]], "not_a_view", []),
        (
            [
                ["view", "public", "top", "1", "note", "text", "v"],
                ["view", "public", "top", "2", "id", "bigint", "v"],
            ],
            "columns_differ",
            ["id", "note"],
        ),
        ([["view", "public", "top", "1", "id", "bigint", "v"]], "columns_differ", ["note"]),
        ([["view", "public", "top", "", "", "", "v"]], "columns_differ", ["id", "note"]),
        (
            [
                ["view", "public", "top", "1", "id", "integer", "v"],
                ["view", "public", "top", "2", "note", "text", "v"],
            ],
            "column_types_differ",
            ["id"],
        ),
    ],
)
def test_each_drift_of_a_carried_view_is_named(lines, problem, columns):
    base = [["view", "public", "base", "1", "id", "bigint", "v"]]
    result = _check(_pg_contract(), _out("aurora_postgresql", base + lines))
    assert [(m.view_id, m.problem, m.columns) for m in result.mismatches] == [
        ("top", problem, columns)
    ]
    assert result.status == "fail"


def test_mysql_compares_names_without_case_and_integers_without_display_width():
    contract = contract_with_views([view("v", engine="aurora_mysql",
                                         columns=[("ID", "int(11)"), ("n", "varchar(20)")])], "aurora_mysql")  # fmt: skip
    lines = [["view", "SHOP", "V", "1", "id", "int", "VIEW"],
             ["view", "SHOP", "V", "2", "N", "VARCHAR(20)", "VIEW"]]  # fmt: skip
    assert _check(contract, _out("aurora_mysql", lines), "aurora_mysql").status == "pass"
    lines[1][5] = "varchar(30)"
    result = _check(contract, _out("aurora_mysql", lines), "aurora_mysql")
    assert [(m.problem, m.columns) for m in result.mismatches] == [("column_types_differ", ["n"])]


@pytest.mark.parametrize(
    "data,message",
    [
        (b"", "empty"),
        (b"\xff\xfe", "not UTF-8"),
        (_out("aurora_postgresql", [], sha="c" * 64), "another step contract"),
        (_out("aurora_mysql", []), "another Aurora engine"),
        (_out("aurora_postgresql", [], end=False), "cut short"),
        (
            _out("aurora_postgresql", [["view", "public", "secret_x", "1", "id", "bigint", "v"]]),
            "does not ask",
        ),
        (
            _out("aurora_postgresql", [["view", "public", "far", "1", "id", "bigint", "v"]]),
            "does not ask",
        ),
        (
            _out("aurora_postgresql", [["view", "public", "top", "x1", "id", "bigint", "v"]]),
            "position",
        ),
        (
            _out("aurora_postgresql", [["view", "public", "top", "1", "id", "bigint", "Q"]]),
            "relation kind",
        ),
        (_out("aurora_postgresql", [["view", "public", "top", "", "id", "", "v"]]), "no position"),
        (
            _out("aurora_postgresql", [["view", "public", "top", "1", "id", "bigint", "v"]] * 2),
            "twice",
        ),
        (
            _out("aurora_postgresql", [["view", "public", "top", "1", "i\x07d", "bigint", "v"]]),
            "control",
        ),
        (_out("aurora_postgresql", [["view", "public", "top", "1", "id"]]), "fields"),
    ],
)
def test_a_malformed_output_is_refused_whole_without_echoing_it(data, message):
    with pytest.raises(RoutineCheckOutputError) as err:
        _check(_pg_contract(), data)
    assert message in str(err.value)
    assert "secret_x" not in str(err.value) and "i\x07d" not in str(err.value)


def test_no_output_yet_reads_none_and_a_section_with_nothing_carried_needs_none(tmp_path):
    contract = _pg_contract()
    assert read_view_check(tmp_path / CHECK_OUTPUT, contract, job_id="job1", database_name="shop",
                           contract_sha=SHA, target_database=None) is None  # fmt: skip
    nothing = contract_with_views([view("far", tables=["public.gone"])])
    result = read_view_check(tmp_path / CHECK_OUTPUT, nothing, job_id="job1", database_name="shop",
                             contract_sha=SHA, target_database=None)  # fmt: skip
    assert (result.status, result.carried, len(result.not_carried)) == ("pass", 0, 1)
