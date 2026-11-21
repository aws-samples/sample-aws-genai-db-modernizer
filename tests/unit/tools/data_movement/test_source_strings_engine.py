# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A source with standard_conforming_strings off (#2333 review, round 3).

PostgreSQL prints a literal for the collecting session's setting: with it off,
``pg_get_viewdef`` and ``pg_get_functiondef`` double each backslash. The step 1 scripts
pin the value the collection recorded, as ``pg_dump`` pins the value it printed under,
so a view and a routine default that return ``C:\\temp`` on the source return exactly that
on the target; the injection payloads stay refused under either pinned value. A
PostgreSQL from ``pgserver`` on a Unix socket in a temp folder (no TCP listener)
holds a source database set off and a target database left at the default; the tests
skip when ``pgserver`` is not installed.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404 -- runs the bundled psql with fixed argv
from pathlib import Path

import pytest

from src.agents.collector.routine_sources import build_routine_sources
from src.tools.data_movement.routine_scripts import DEPLOY_FILE as ROUTINES_DEPLOY
from src.tools.data_movement.view_scripts import DEPLOY_FILE as VIEWS_DEPLOY
from tests.pg_server import PgServerUnavailable, start_pg_server
from tests.unit.tools.data_movement.routine_fixtures import (
    contract_with_routines,
    migration,
    pg_source,
    row,
)
from tests.unit.tools.data_movement.test_routine_scripts import _job as routine_job
from tests.unit.tools.data_movement.test_routine_scripts import _write as write_routines
from tests.unit.tools.data_movement.view_fixtures import (
    contract_with_views,
    job,
    pg_view_source,
    view,
    write,
)

pgserver = pytest.importorskip("pgserver")

_ROOT = Path(__file__).resolve().parents[4]
_SOURCE = r"""
CREATE TABLE public.orders (id bigint PRIMARY KEY, note text);
CREATE VIEW public.paths AS SELECT 'C:\\temp'::text AS id;
CREATE FUNCTION public.path_of(p text DEFAULT E'C:\\temp') RETURNS text LANGUAGE sql AS $$ SELECT p $$;
"""


@pytest.fixture(scope="module")
def server():
    try:
        srv = start_pg_server()
    except PgServerUnavailable as exc:  # the one failure that skips
        pytest.skip(f"no pgserver PostgreSQL here: {exc}")
    srv.psql("CREATE DATABASE src_off; CREATE DATABASE tgt;")
    srv.psql("ALTER DATABASE src_off SET standard_conforming_strings = off;")
    ran = _psql(srv, "src_off", "-v", "ON_ERROR_STOP=1", "-c", _SOURCE)
    assert ran.returncode == 0, ran.stderr
    yield srv
    srv.cleanup()


def _psql(server, database: str, *args: str) -> subprocess.CompletedProcess[str]:
    psql = Path(pgserver.__file__).parent / "pginstall" / "bin" / "psql"
    return subprocess.run([str(psql), server.get_uri(database), "-X", *args], capture_output=True,
                          text=True, check=False)  # fmt: skip


def _value(server, database: str, sql: str) -> str:
    ran = _psql(server, database, "-At", "-c", sql)
    assert ran.returncode == 0, ran.stderr
    return ran.stdout.rstrip("\n")


def _recorded(store, value: str | None) -> None:
    settings = {"standard_conforming_strings": value} if value else {}
    store.write_json("shop/job1/collector/output.json", {
        "metadata": {"source_database": {"engine": "postgresql", "settings": settings}}})  # fmt: skip


def _collected_view(server) -> dict:
    script = (_ROOT / "scripts/collect-postgresql.sql").read_text()
    cte = script.split("views_data AS (", 1)[1].split("\n),\n", 1)[0]
    rows = json.loads(
        _value(
            server,
            "src_off",
            f"WITH views_data AS ({cte}\n) SELECT data::jsonb::text FROM views_data",
        )
    )
    return next(r for r in rows if r["view_name"] == "paths")


def test_a_view_collected_under_off_returns_its_value_exactly_on_the_target(server, tmp_path):
    assert _value(server, "src_off", "SELECT id FROM public.paths") == "C:\\temp"
    row = _collected_view(server)
    assert "'C:\\\\temp'" in row["definition"]  # the source printed it doubled
    (source,) = build_routine_sources("postgresql", procedures=[], triggers=[], views=[row]).sources
    contract = contract_with_views(
        [view("paths", columns=[("id", "text")], tables=["public.orders"])]
    )
    store = job(tmp_path, [source])
    _recorded(store, "off")
    assert write(tmp_path, store, contract).in_script == 1
    deploy = tmp_path / "step" / VIEWS_DEPLOY
    assert "SET standard_conforming_strings = off;" in deploy.read_text()
    ran = _psql(server, "tgt", "-f", str(deploy))
    assert ran.returncode == 0, ran.stderr
    assert _value(server, "tgt", "SELECT id FROM public.paths") == "C:\\temp"
    _value(server, "tgt", "DROP VIEW public.paths")


def test_a_routine_default_collected_under_off_returns_its_value_exactly_on_the_target(
    server, tmp_path
):
    text = _value(
        server, "src_off", "SELECT pg_get_functiondef('public.path_of(text)'::regprocedure)"
    )
    assert "'C:\\\\temp'" in text
    artifact = migration([row("public.path_of(text)", specific="path_of_1", signature="text")])
    store = routine_job(tmp_path, artifact, [pg_source("path_of_1", text)])
    _recorded(store, "off")
    assert write_routines(tmp_path, store, contract_with_routines(artifact)).in_script == 1
    deploy = tmp_path / "step" / ROUTINES_DEPLOY
    assert "SET standard_conforming_strings = off;" in deploy.read_text()
    ran = _psql(server, "tgt", "-f", str(deploy))
    assert ran.returncode == 0, ran.stderr
    assert _value(server, "tgt", "SELECT public.path_of()") == "C:\\temp"
    _value(server, "tgt", "DROP FUNCTION public.path_of(text)")


def test_without_a_recorded_setting_the_pin_is_on_and_says_so(tmp_path):
    text = 'CREATE OR REPLACE VIEW "public"."v" ("id") AS\n SELECT 1::bigint AS id;'
    store = job(tmp_path, [pg_view_source("v", text)])
    write(tmp_path, store, contract_with_views([view("v")]))
    deploy = (tmp_path / "step" / VIEWS_DEPLOY).read_text()
    assert "SET standard_conforming_strings = on;" in deploy
    assert "the collection does not record the source's setting" in deploy


@pytest.mark.parametrize("recorded", ["on", "off"])
def test_the_injection_payloads_stay_refused_under_either_pinned_value(server, tmp_path, recorded):
    marker = Path("/tmp/v2082-pwned")
    marker.unlink(missing_ok=True)
    hostile = ('CREATE OR REPLACE VIEW "public"."evil" ("a", "id") AS\n'
               f"SELECT 'x\\' AS a, '\\! touch {marker} #' AS id;")  # fmt: skip
    doubled = ('CREATE OR REPLACE VIEW "public"."evil2" ("a", "id") AS\n'
               f"SELECT 'x\\\\' AS a, '\\! touch {marker} #' AS id;")  # fmt: skip
    _value(server, "tgt", "CREATE TABLE IF NOT EXISTS public.audit_log (msg text)")
    contract = contract_with_views([view("evil", columns=[("a", "text"), ("id", "text")]),
                                    view("evil2", columns=[("a", "text"), ("id", "text")])])  # fmt: skip
    store = job(tmp_path, [pg_view_source("evil", hostile), pg_view_source("evil2", doubled)])
    _recorded(store, recorded)
    scripts = write(tmp_path, store, contract)
    # The first ends elsewhere under the other reading: refused. The second reads alike both
    # ways (its \\! sits inside a literal either way), so it deploys as harmless data.
    assert scripts.refused == [("evil", "statement_depends_on_backslashes")]
    assert scripts.in_script == 1
    for setting in ("on", "off"):
        _value(server, "tgt", f"ALTER DATABASE tgt SET standard_conforming_strings = {setting}")
        ran = _psql(server, "tgt", "-f", str(tmp_path / "step" / VIEWS_DEPLOY))
        assert ran.returncode == 0, ran.stderr
        assert not marker.exists()
        assert _value(server, "tgt", "SELECT to_regclass('public.audit_log') IS NOT NULL") == "t"
    _value(server, "tgt", "ALTER DATABASE tgt RESET standard_conforming_strings")
    _value(server, "tgt", "DROP VIEW IF EXISTS public.evil2")
