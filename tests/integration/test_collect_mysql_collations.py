# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``collect-mysql.sql`` and the live collector read the same collations on a real server (#2276).

Needs ``mysqld`` and the ``mysql`` client on PATH (the throwaway Unix-socket server of
``test_routine_scripts_mysql.py``). A database on utf8mb4_unicode_ci holds a table on it,
with one column on utf8mb4_bin and one in latin1, and a table on latin1. Both paths record
the database's defaults and, for exactly those two columns, the column's own. Run it with
``uv run pytest -m integration tests/integration/test_collect_mysql_collations.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.agents.collector.mysql_collector import _build_columns
from src.agents.collector.offline_ingest import build_offline_collector_output
from src.tools.aws.ssm_executor import _parse_tabular_output
from src.tools.database.mysql_tools import MySQLRemoteCollector
from src.tools.database.offline_collection_check import read_collection_text
from src.tools.database.offline_parser import parse_offline_collection
from tests.integration.test_routine_scripts_mysql import _mysql, mysql_socket  # noqa: F401

pytestmark = pytest.mark.integration

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "collect-mysql.sql"
SETUP = """
    DROP DATABASE IF EXISTS coll;
    CREATE DATABASE coll CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
    CREATE TABLE coll.t (id int PRIMARY KEY, a varchar(10), b varchar(10) COLLATE utf8mb4_bin,
                         c text CHARACTER SET latin1, n int);
    CREATE TABLE coll.u (id int PRIMARY KEY, d varchar(5)) DEFAULT CHARSET=latin1;
"""  # fmt: skip
EXPECTED_COLUMNS = {
    ("t", "id"): (None, None), ("t", "a"): (None, None), ("t", "n"): (None, None),
    ("t", "b"): ("utf8mb4", "utf8mb4_bin"), ("t", "c"): ("latin1", "latin1_swedish_ci"),
    ("u", "id"): (None, None), ("u", "d"): (None, None),
}  # fmt: skip
DATABASE = {"default_character_set": "utf8mb4", "default_collation": "utf8mb4_unicode_ci"}


@pytest.fixture(scope="module")
def coll(mysql_socket):  # noqa: F811
    client, sock = mysql_socket
    done = _mysql(client, sock, SETUP)
    assert done.returncode == 0, done.stderr.decode()
    yield client, sock
    _mysql(client, sock, "DROP DATABASE IF EXISTS coll;")


def _offline(client, sock) -> dict:
    done = _mysql(client, sock, None, "-N", "--raw", "-D", "coll",
                  "--init-command=SET @rate_sample_seconds=0", stdin=SCRIPT)  # fmt: skip
    assert done.returncode == 0, done.stderr.decode()
    raw = read_collection_text(done.stdout.decode(), source="collection.json")
    return build_offline_collector_output(
        parse_offline_collection(raw), job_id="j", database_name="coll", s3_key="k"
    )


class _Live(MySQLRemoteCollector):
    def __init__(self, client, sock):  # the client on the socket, as SSM runs it
        self.client, self.sock = client, sock

    def _query(self, sql: str) -> list[dict]:
        done = _mysql(self.client, self.sock, sql, "-B", "-D", "coll")
        assert done.returncode == 0, done.stderr.decode()
        return _parse_tabular_output(done.stdout.decode(), "mysql")


def test_the_script_and_the_live_collector_record_the_same_collations(coll):
    output = _offline(*coll)
    source = output["metadata"]["source_database"]
    assert {k: source[k] for k in DATABASE} == DATABASE
    offline = {(t["table_name"], c["column_name"]): (c["character_set"], c["collation"])
               for t in output["database_schema"]["tables"] for c in t["columns"]}  # fmt: skip
    assert offline == EXPECTED_COLUMNS

    live = _Live(*coll)
    assert live.get_database_charset() == DATABASE
    by_table = {(t, c.column_name): (c.character_set, c.collation)
                for t in ("t", "u") for c in _build_columns(live.collect_columns(t))}  # fmt: skip
    assert by_table == offline
