# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""PostgreSQL enum labels, read from a real server and created again (#1834).

Both enum queries (``scripts/collect-postgresql.sql``'s ``enum_types_data`` and the live
collector's ``collect_enum_types``) run against a PostgreSQL started by ``pgserver`` on a
Unix socket in a temp folder (no TCP listener): the labels come back in sort order, which
``ALTER TYPE ... ADD VALUE BEFORE`` makes differ from creation order. Step 1's
``CREATE TYPE`` for those labels (quotes and backslashes included) runs on the same server
and gives the same labels back. The tests skip when ``pgserver`` cannot start.
"""

from __future__ import annotations

import pytest

from src.tools.database.postgres_tools import PostgreSQLRemoteCollector
from src.tools.transformation_contract.pg_enums import EnumType, create_type_sql
from tests.unit.tools.database.test_postgres_foreign_keys_live_server import _ROOT, _run

pgserver = pytest.importorskip("pgserver")

_SCHEMA = r"""
CREATE SCHEMA other;
CREATE TYPE mood AS ENUM ('sad', 'happy');
ALTER TYPE mood ADD VALUE 'ok' BEFORE 'happy';
CREATE TYPE other.odd AS ENUM ('it''s', 'back\slash', 'a,b');
CREATE DOMAIN not_an_enum AS text;
"""
_EXPECTED = [
    {"schema_name": "other", "type_name": "odd", "labels": ["it's", "back\\slash", "a,b"]},
    {"schema_name": "public", "type_name": "mood", "labels": ["sad", "ok", "happy"]},
]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    try:
        srv = pgserver.get_server(tmp_path_factory.mktemp("pgdata"), cleanup_mode="delete")
    except Exception as exc:  # no bundled server for this platform, or it cannot start
        pytest.skip(f"pgserver cannot start a PostgreSQL here: {exc}")
    srv.psql("\\set ON_ERROR_STOP on\n" + _SCHEMA)
    yield srv
    srv.cleanup()


def test_the_offline_query_lists_each_enum_with_its_labels_in_sort_order(server):
    script = (_ROOT / "scripts/collect-postgresql.sql").read_text()
    cte = script.split("enum_types_data AS (", 1)[1].split("\n)\n", 1)[0]
    sql = (f"WITH enum_types_data AS ({cte}\n) "
           "SELECT jsonb_array_elements(data::jsonb) AS e FROM enum_types_data")  # fmt: skip
    assert [r["e"] for r in _run(server, sql, None)] == _EXPECTED


def test_the_live_collector_lists_the_same_enums(server):
    class Live(PostgreSQLRemoteCollector):
        def _query(self, sql):
            return _run(server, sql.strip().rstrip(";"), None)

    assert Live.__new__(Live).collect_enum_types() == _EXPECTED


def test_step_1_creates_the_same_type_again(server):
    odd = _EXPECTED[0]
    created = EnumType("public", "odd_copy", tuple(odd["labels"]))
    server.psql("\\set ON_ERROR_STOP on\nSET standard_conforming_strings = on;\n"
                + create_type_sql(created) + "\n")  # fmt: skip
    rows = _run(server, "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                        "WHERE t.typname = 'odd_copy' ORDER BY e.enumsortorder", None)  # fmt: skip
    assert [r["enumlabel"] for r in rows] == odd["labels"]
