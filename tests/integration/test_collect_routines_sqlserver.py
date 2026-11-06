# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""collect-sqlserver.sql against a real SQL Server (#1652). Not gated: runs only when
MODERNIZER_SQLSERVER_DSN is set ("host,port;user;password;database") and sqlcmd is
installed.

A server to run it against: SQL Server 2022 in a Linux container
(``mcr.microsoft.com/mssql/server:2022-latest``, published on 127.0.0.1 only) loaded
with a schema that has procedures and triggers, for example DNN Platform's install
scripts (``DNN Platform/Website/Providers/DataProviders/SqlDataProvider/*.SqlDataProvider``,
with ``{databaseOwner}`` and ``{objectQualifier}`` replaced); no IIS is needed. Run
``uv run pytest -m integration tests/integration/test_collect_routines_sqlserver.py``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404 -- runs sqlcmd with fixed argv
from pathlib import Path

import pytest

from src.tools.database.collection_text import extract_json_object
from src.tools.database.offline_parser import parse_offline_collection

pytestmark = pytest.mark.integration

DSN = os.environ.get("MODERNIZER_SQLSERVER_DSN")
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "collect-sqlserver.sql"


@pytest.mark.skipif(not DSN or not shutil.which("sqlcmd"), reason="needs a SQL Server and sqlcmd")
def test_the_script_collects_routine_bodies_events_and_counts():
    assert DSN is not None
    host, user, password, database = DSN.split(";")
    script = SCRIPT.read_text().replace(
        "DECLARE @rate_sample_seconds INT = 300;", "DECLARE @rate_sample_seconds INT = 0;"
    )
    out = subprocess.run(  # nosec B603
        ["sqlcmd", "-S", host, "-U", user, "-d", database, "-C", "-y", "0"],
        input=script, check=True, capture_output=True, text=True, timeout=600,
        env={**os.environ, "SQLCMDPASSWORD": password},
    ).stdout  # fmt: skip
    raw = json.loads(extract_json_object(out, source="sqlcmd"))
    parsed = parse_offline_collection(raw)
    assert any(p.get("definition") for p in parsed["procedures"])
    assert all(t.get("events") for t in parsed["triggers"])
    assert parsed["routine_stats_status"]["status"] in ("tracked", "not_tracked")
