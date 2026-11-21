# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``collect-mysql.sql`` reads each key's referenced database and each database's collation (#2531).

Needs ``mysqld`` and the ``mysql`` client on PATH (the throwaway Unix-socket server of
``test_routine_scripts_mysql.py``). The collected database ``fkdb`` (utf8mb4_unicode_ci)
holds a key into its own ``parent`` and one into ``fkref.parent``, a database on
latin1_swedish_ci that also has a ``parent``. The script reports each key's database from
``KEY_COLUMN_USAGE.REFERENCED_TABLE_SCHEMA`` and both databases' defaults from
``SCHEMATA``; the live collector reports the same keys. Run it with
``uv run pytest -m integration tests/integration/test_collect_mysql_foreign_key_schema.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.agents.collector.offline_ingest import build_offline_collector_output
from src.tools.aws.ssm_executor import _parse_tabular_output
from src.tools.database.mysql_tools import MySQLRemoteCollector
from src.tools.database.offline_collection_check import read_collection_text
from src.tools.database.offline_parser import parse_offline_collection
from tests.integration.test_routine_scripts_mysql import _mysql, mysql_socket  # noqa: F401

pytestmark = pytest.mark.integration

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "collect-mysql.sql"
SETUP = """
    DROP DATABASE IF EXISTS fkdb;
    DROP DATABASE IF EXISTS fkref;
    DROP DATABASE IF EXISTS fkother;
    CREATE DATABASE fkdb CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
    CREATE DATABASE fkref CHARACTER SET latin1 COLLATE latin1_swedish_ci;
    CREATE DATABASE fkother CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;
    CREATE TABLE fkdb.parent (id int PRIMARY KEY);
    CREATE TABLE fkref.parent (id int PRIMARY KEY);
    CREATE TABLE fkdb.child (id int PRIMARY KEY, own_id int, ref_id int,
      CONSTRAINT fk_own FOREIGN KEY (own_id) REFERENCES fkdb.parent (id),
      CONSTRAINT fk_ref FOREIGN KEY (ref_id) REFERENCES fkref.parent (id));
"""  # fmt: skip
KEYS = {"fk_own": ("parent", "fkdb"), "fk_ref": ("parent", "fkref")}
SCHEMAS = [
    {"schema_name": "fkdb", "default_character_set": "utf8mb4",
     "default_collation": "utf8mb4_unicode_ci"},
    {"schema_name": "fkref", "default_character_set": "latin1",
     "default_collation": "latin1_swedish_ci"},
]  # fmt: skip


@pytest.fixture(scope="module")
def fkdb(mysql_socket):  # noqa: F811
    client, sock = mysql_socket
    done = _mysql(client, sock, SETUP)
    assert done.returncode == 0, done.stderr.decode()
    yield client, sock
    _mysql(client, sock, "DROP DATABASE IF EXISTS fkdb; DROP DATABASE IF EXISTS fkref; "
                         "DROP DATABASE IF EXISTS fkother;")  # fmt: skip


def _offline(client, sock) -> dict:
    done = _mysql(client, sock, None, "-N", "--raw", "-D", "fkdb",
                  "--init-command=SET @rate_sample_seconds=0", stdin=SCRIPT)  # fmt: skip
    assert done.returncode == 0, done.stderr.decode()
    raw = read_collection_text(done.stdout.decode(), source="collection.json")
    return build_offline_collector_output(
        parse_offline_collection(raw), job_id="j", database_name="fkdb", s3_key="k"
    )


class _Live(MySQLRemoteCollector):
    def __init__(self, client, sock):  # the client on the socket, as SSM runs it
        self.client, self.sock = client, sock

    def _query(self, sql: str) -> list[dict]:
        done = _mysql(self.client, self.sock, sql, "-B", "-D", "fkdb")
        assert done.returncode == 0, done.stderr.decode()
        return _parse_tabular_output(done.stdout.decode(), "mysql")


def test_the_script_reports_each_keys_database_and_each_databases_collation(fkdb):
    output = _offline(*fkdb)
    (child,) = [t for t in output["database_schema"]["tables"] if t["table_name"] == "child"]
    got = {fk["constraint_name"]: (fk["referenced_table"], fk["referenced_schema"])
           for fk in child["foreign_keys"]}  # fmt: skip
    assert got == KEYS
    # The collected database and the one its keys reference; never another database.
    assert output["metadata"]["source_database"]["schemas"] == SCHEMAS


def test_the_live_collector_reports_the_same_keys(fkdb):
    got = {fk["constraint_name"]: (fk["referenced_table"], fk["referenced_schema"])
           for fk in _Live(*fkdb).collect_foreign_keys("child")}  # fmt: skip
    assert got == KEYS
