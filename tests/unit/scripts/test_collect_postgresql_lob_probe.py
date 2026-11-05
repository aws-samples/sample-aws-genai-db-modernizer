# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""collect-postgresql.sql runs whole on a server with or without XML support (#1918).

The LOB measurement reads each column's longest value through ``query_to_xml``, which needs
a server built with XML support. Without it, the collection must still complete and leave
``max_value_bytes`` null ("not measured"); with it, the value is measured.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

pgserver = pytest.importorskip("pgserver")

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "collect-postgresql.sql"


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    try:
        srv = pgserver.get_server(tmp_path_factory.mktemp("pgdata"), cleanup_mode="delete")
    except Exception as exc:  # no bundled server for this platform, or it cannot start
        pytest.skip(f"pgserver cannot start a PostgreSQL here: {exc}")
    srv.psql(
        "\\set ON_ERROR_STOP on\n"
        "CREATE TABLE public.pg_stat_statements (userid oid, dbid oid, queryid bigint,"
        " toplevel bool, query text, calls bigint, total_exec_time float8, min_exec_time float8,"
        " max_exec_time float8, rows bigint, shared_blks_hit bigint, shared_blks_read bigint,"
        " blk_read_time float8, blk_write_time float8, temp_blks_read bigint,"
        " temp_blks_written bigint);\n"
        "CREATE TABLE public.notes (id int PRIMARY KEY, body text);\n"
        "INSERT INTO public.notes VALUES (1, repeat('x', 40)), (2, repeat('y', 7));\n"
    )
    yield srv
    srv.cleanup()


def _has_xml(server) -> bool:
    try:
        server.psql("\\set ON_ERROR_STOP on\nSELECT query_to_xml('SELECT 1', false, true, '');\n")
    except (subprocess.CalledProcessError, RuntimeError):
        return False
    return True


def _body_column(server) -> dict:
    head = "SET search_path = public, pg_catalog;\n\\t on\n\\a\n\\set rate_sample_seconds 0\n"
    out = server.psql(head + SCRIPT.read_text())
    start = out.find('{"collection_version"')
    assert start >= 0, out[-2000:]  # the collection completed
    data = json.loads(out[start:])
    found = [
        c
        for t in data.get("tables", []) + data.get("database_schema", {}).get("tables", [])
        for c in t.get("columns", [])
        if c.get("column_name") == "body"
    ] or [c for c in data.get("columns", []) if c.get("column_name") == "body"]
    assert found, "the notes.body column is in the collection"
    column: dict = found[0]
    return column


def test_the_collection_completes_and_measures_lobs_only_with_xml_support(server):
    column = _body_column(server)
    if _has_xml(server):
        assert column["max_value_bytes"] == 40
    else:
        assert column.get("max_value_bytes") is None  # not measured, not a failure
