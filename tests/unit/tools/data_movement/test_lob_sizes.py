# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's read-only ``lob-sizes.sql`` and the strict reader of its output (#2123).

A live WordPress run at 64 KB truncated four rows (``wp_options.option_value`` up to 79,934
bytes) with no task error: the MySQL source endpoint never stops on a truncation. A moved MySQL
table whose LOB columns the collection did not measure now stops preflight, and step 1 writes
a script the customer runs; /move reads only its output, refusing a bad file whole.
"""

from __future__ import annotations

import re

import pytest

from src.contracts.data_movement_plan import LobTable
from src.tools.data_movement.dms_preflight import check_lob_sizes
from src.tools.data_movement.dms_step import render_files
from src.tools.data_movement.lob_sizes import (
    END,
    MAGIC,
    LobSizesError,
    layout_sha,
    measure_columns,
    parse_output,
    render_script,
)

from .dms_step_fixtures import make_step1_inputs

SHA = "a" * 64
STARTED = "2026-11-11T10:00:00.123456"


def _t(table, columns, source="unmeasured", mode="limited"):
    return LobTable(table=table, mode=mode, columns=columns, max_size_kb=64 if mode == "limited" else None,
                    size_source=source, reason="LOB size not measured")  # fmt: skip


def _columns():
    return measure_columns([_t("wordpress.wp_posts", ["post_content", "post_content_filtered"]),
                            _t("wordpress.wp_options", ["option_value"]),
                            _t("wordpress.wp_users", ["bio"], source="measured")],
                           "mysql", {"wordpress.wp_posts", "wordpress.wp_options", "wordpress.wp_users"})  # fmt: skip


def _output(columns, *, sha=SHA, layout=None, lines=None, end=True):
    head = f"{MAGIC}\t1\t{sha}\t{layout or layout_sha(columns)}\t{STARTED}\n"
    body = (
        lines
        if lines is not None
        else "".join(f"column\t{c.n}\t{c.k}\t{1000 * c.n + c.k}\t7\n" for c in columns)
    )
    return (head + body + (f"{END}\t\t\t\t\n" if end else "")).encode()


def test_only_unmeasured_mysql_tables_are_measured_in_table_then_column_order():
    cols = _columns()
    assert [(c.n, c.k, c.table, c.column) for c in cols] == [
        (1, 1, "wordpress.wp_options", "option_value"),
        (2, 1, "wordpress.wp_posts", "post_content"),
        (2, 2, "wordpress.wp_posts", "post_content_filtered"),
    ]
    # PostgreSQL keeps #1918's finding: nothing to measure there.
    assert measure_columns([_t("public.posts", ["raw"])], "postgresql", {"public.posts"}) == []


def test_a_plan_written_before_2123_lists_full_mysql_tables_and_they_need_measuring():
    legacy = LobTable(table="wordpress.wp_posts", mode="full", columns=["post_content"],
                      reason="A LOB column declares up to 16777216 KB")  # fmt: skip
    assert [c.table for c in measure_columns([legacy], "mysql", {"wordpress.wp_posts"})] == [
        "wordpress.wp_posts"
    ]


def test_the_script_is_read_only_quotes_every_name_and_reads_each_table_once():
    cols = measure_columns([_t("shop.we`ird", ["a`b", "c"])], "mysql", {"shop.we`ird"})
    sql = render_script(cols, SHA)
    assert (
        "SET SESSION TRANSACTION READ ONLY;" in sql
        and sql.count("START TRANSACTION READ ONLY;") == 1
    )
    assert "FROM `shop`.`we``ird`" in sql and "MAX(LENGTH(`a``b`))" in sql and "COUNT(`c`)" in sql
    assert sql.count(" FROM `shop`") == 1  # one scan for both columns
    assert not re.search(r"\b(?:INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|GRANT|TRUNCATE|REPLACE|SET GLOBAL)\b",
                         sql.upper())  # fmt: skip
    assert f"'{MAGIC}', '1', '{SHA}', '{layout_sha(cols)}'" in sql and f"'{END}'" in sql


def test_a_name_with_a_control_character_is_refused():
    with pytest.raises(LobSizesError, match="control character"):
        measure_columns([_t("shop.t", ["a\tb"])], "mysql", {"shop.t"})


def test_a_good_output_parses_into_lengths_by_position():
    cols = _columns()
    started, rows = parse_output(
        _output(cols), name="lob-sizes.tsv", contract_sha=SHA, columns=cols
    )
    assert started == STARTED and rows == {(1, 1): (1001, 7), (2, 1): (2001, 7), (2, 2): (2002, 7)}


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (lambda c: _output(c, sha="b" * 64), "another step contract"),
        (lambda c: _output(c, layout="c" * 64), "other tables or columns"),
        (lambda c: _output(c, end=False), "no end line"),
        (lambda c: _output(c, lines="column\t1\t1\t5\t1\n"), "no line for table 2 column 1"),
        (
            lambda c: _output(c, lines="column\t1\t1\t5\t1\ncolumn\t1\t1\t5\t1\n"),
            "the same column twice",
        ),
        (lambda c: _output(c, lines="column\t9\t1\t5\t1\n"), "does not ask for"),
        (lambda c: _output(c, lines="column\t1\t1\t-5\t1\n"), "not a column line"),
        (lambda c: _output(c, lines="column\t1\t1\t5\n"), "4 tab-separated fields"),
        (lambda c: _output(c, lines="column\t1\t1\t5\x1b\t1\n"), "control character"),
        (lambda c: b"\xff\xfe", "not UTF-8"),
        (lambda c: b"", "empty"),
        (lambda c: b"SECRET-ROW-VALUE\t1\t2\t3\t4\n", "not the header"),
    ],
)
def test_a_bad_output_is_refused_whole_naming_the_line_never_its_content(change, problem):
    cols = _columns()
    data = change(cols)
    with pytest.raises(LobSizesError, match=problem) as refused:
        parse_output(data, name="lob-sizes.tsv", contract_sha=SHA, columns=cols)
    assert "SECRET" not in str(refused.value) and "\x1b" not in str(refused.value)


def test_step_1_writes_the_script_while_a_mysql_lob_table_is_unmeasured():
    files = render_files(make_step1_inputs())
    assert "lob-sizes.sql" in files and "lob-sizes.md" in files
    assert "MAX(LENGTH(`bio`))" in files["lob-sizes.sql"]
    assert "--batch --skip-column-names --force < lob-sizes.sql" in files["lob-sizes.md"]
    assert "lob-sizes.sql" not in render_files(make_step1_inputs(measured=True))
    assert "lob-sizes.sql" not in render_files(make_step1_inputs("aurora_postgresql"))


def test_preflight_fails_on_an_unmeasured_mysql_lob_table_and_says_what_to_run():
    row = check_lob_sizes(make_step1_inputs(), None, None, {})
    assert row.status == "fail" and "shop.customers" in row.summary
    assert "lob-sizes.sql" in (row.message or "") and "run_dms_step.py lob-sizes" in (
        row.message or ""
    )
    assert '"lob_sizes": "skip"' in (row.message or "")
    assert check_lob_sizes(make_step1_inputs(measured=True), None, None, {}).status == "pass"
