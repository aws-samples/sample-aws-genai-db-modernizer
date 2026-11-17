# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The wordpress demo routine set installs on MySQL and collects back to the bundled set
(#1954).

``docs/examples/wordpress/wordpress-routines-install.sql`` (rendered by
``scripts/render_sample_routines.py``) is what the step 1 rehearsal runs on the sample's
MySQL source. Run with the mysql client in a fresh database holding the tables the
routines name, then collected by ``scripts/collect-mysql.sql``, it must give the same
routine sources, sessions included, that the bundled ``wordpress-routines.json`` gives the
deterministic e2e; the remove script must leave none of them.

Set MYSQL_ROUTINE_TEST_DSN as ``tests/integration/test_mysql_routine_create_round_trip.py``
says (a local ``mysqld --skip-networking`` on a socket works) and run
``uv run pytest -m integration tests/integration/test_sample_routine_install_mysql.py``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404 -- runs the mysql client with fixed argv
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from src.agents.collector.routine_sources import (
    build_routine_sources,
    routine_sources_from_collection,
)

pytest.importorskip("mysql.connector")

from tests.integration.test_mysql_routine_create_round_trip import (  # noqa: E402
    _collect,
    _connect,
    _dsn,
    _script_collection,
)

pytestmark = pytest.mark.integration

EXAMPLES = Path(__file__).resolve().parents[2] / "docs" / "examples" / "wordpress"
# The tables and columns the routines name; on the rehearsal source they are the shop's.
_TABLES = [
    "CREATE TABLE wp_posts (ID bigint unsigned PRIMARY KEY, comment_count bigint NOT NULL DEFAULT 0)",
    "CREATE TABLE wp_comments (comment_ID bigint unsigned PRIMARY KEY, comment_post_ID bigint unsigned NOT NULL)",
    "CREATE TABLE wp_actionscheduler_logs (log_id bigint unsigned PRIMARY KEY, log_date_gmt datetime)",
]  # fmt: skip


@pytest.fixture
def database() -> Iterator[str]:
    admin = _connect("MYSQL_ROUTINE_TEST_DSN")
    name = f"rt_demo_{uuid4().hex[:10]}"
    cursor = admin.cursor()
    try:
        cursor.execute(  # nosemgrep: sqlalchemy-execute-raw-query, formatted-sql-query -- disposable test server
            f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_520_ci"
        )
        conn = _connect("MYSQL_ROUTINE_TEST_DSN", name)
        for statement in _TABLES:
            conn.cursor().execute(statement)  # nosemgrep: sqlalchemy-execute-raw-query -- fixed
        conn.close()
        yield name
    finally:
        cursor.execute(  # nosemgrep: sqlalchemy-execute-raw-query, formatted-sql-query -- disposable test server
            f"DROP DATABASE IF EXISTS `{name}`"
        )
        admin.close()


def _client(database: str, script: Path) -> None:
    if not shutil.which("mysql"):
        pytest.skip("needs the mysql client")
    host, port, user, password = _dsn("MYSQL_ROUTINE_TEST_DSN")
    where = ["--socket", host] if host.startswith("/") else ["-h", host, "-P", str(port)]
    subprocess.run(  # nosec B603 -- the mysql client with fixed argv
        ["mysql", "-u", user, *where, "-D", database],
        input=script.read_text(encoding="utf-8"), capture_output=True, text=True, check=True,
        timeout=120, env={**os.environ, "MYSQL_PWD": password},
    )  # fmt: skip


def _key(build: Any) -> list[tuple[Any, ...]]:
    return sorted(
        (s.kind, s.specific_name, s.create_statement, s.session.sql_mode,
         s.session.character_set_client, s.session.collation_connection,
         s.session.database_collation, s.session.time_zone)
        for s in build.sources
    )  # fmt: skip


def test_the_wordpress_install_collects_back_to_the_bundled_set(database: str) -> None:
    install = EXAMPLES / "wordpress-routines-install.sql"
    _client(database, install)
    _client(database, install)  # it can run again
    collected = routine_sources_from_collection(
        _script_collection("MYSQL_ROUTINE_TEST_DSN", database)
    )
    data = json.loads((EXAMPLES / "wordpress-routines.json").read_text())
    bundled = build_routine_sources(
        "mysql", procedures=data["procedures"], triggers=data["triggers"],
        events=data["events"], parameters=data["routine_parameters"],
    )  # fmt: skip
    assert collected.statuses == {}
    assert _key(collected) == _key(bundled)

    _client(database, EXAMPLES / "wordpress-routines-remove.sql")
    left = _collect(_connect("MYSQL_ROUTINE_TEST_DSN", database))
    assert [r for rows in left.values() for r in rows if r.get("schema_name") == database] == []
