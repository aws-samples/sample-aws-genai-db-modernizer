# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's view scripts on a real PostgreSQL (#2082), run as the customer runs them.

A PostgreSQL started by ``pgserver`` on a Unix socket in a temp folder (no TCP listener)
plays the Aurora target. The views are read from the server as the collection script's
section 7 reads them, their CREATE built (``view_sources``), the views dropped, and then
``psql -X -f views-deploy.sql`` runs twice and ``psql -X -f views-check.sql -o
views-check.tsv`` once, exactly as ``views.md`` says. The check output must confirm every
carried view; then each kind of drift is made by hand and named. A routine that reads a
carried view deploys after it. The tests skip when ``pgserver`` is not installed or cannot
start. The MySQL twin needs a local ``mysqld``: ``tests/integration/test_view_scripts_mysql.py``.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404 -- runs the bundled psql with fixed argv
from pathlib import Path

import pytest

from src.agents.collector.routine_sources import build_routine_sources
from src.tools.data_movement.view_check import read_view_check
from src.tools.data_movement.view_scripts import CHECK_FILE, CHECK_OUTPUT, DEPLOY_FILE
from tests.unit.tools.data_movement.view_fixtures import (
    SENTINEL,
    SHA,
    contract_with_views,
    job,
    view,
    write,
)

pgserver = pytest.importorskip("pgserver")

_ROOT = Path(__file__).resolve().parents[4]
_SETUP = f"""
CREATE TABLE public.orders (id bigint PRIMARY KEY, note text);
CREATE SCHEMA reports;
CREATE VIEW public.base AS SELECT id, note FROM orders WHERE note <> '{SENTINEL}';
CREATE VIEW reports.top (order_id, note) AS SELECT b.id, b.note FROM public.base b
  WITH CASCADED CHECK OPTION;
"""


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    try:
        srv = pgserver.get_server(tmp_path_factory.mktemp("pgdata"), cleanup_mode="delete")
    except Exception as exc:  # no bundled server for this platform, or it cannot start
        pytest.skip(f"pgserver cannot start a PostgreSQL here: {exc}")
    srv.psql("\\set ON_ERROR_STOP on\n" + _SETUP)
    yield srv
    srv.cleanup()


def _psql(server, *args: str) -> subprocess.CompletedProcess[str]:
    psql = Path(pgserver.__file__).parent / "pginstall" / "bin" / "psql"
    return subprocess.run([str(psql), server.get_uri(), "-X", *args], capture_output=True,
                          text=True, check=False)  # fmt: skip


def _collected(server) -> list[dict]:
    script = (_ROOT / "scripts/collect-postgresql.sql").read_text()
    cte = script.split("views_data AS (", 1)[1].split("\n),\n", 1)[0]
    out = server.psql(
        f"\\set QUIET on\n\\t on\n\\a\nWITH views_data AS ({cte}\n) SELECT data::jsonb::text FROM views_data;\n"
    )
    rows: list[dict] = json.loads([ln for ln in out.splitlines() if ln.strip()][-1])
    return rows


def test_the_scripts_deploy_twice_and_confirm_on_postgresql_then_name_each_drift(server, tmp_path):
    rows = _collected(server)
    sources = build_routine_sources("postgresql", procedures=[], triggers=[], views=rows).sources
    assert sorted(s.specific_name for s in sources) == ["base", "top"]
    server.psql("DROP VIEW reports.top; DROP VIEW public.base; DROP SCHEMA reports;")
    cols = {r["view_name"]: [(c["name"], c["data_type"]) for c in r["columns"]] for r in rows}
    contract = contract_with_views([
        view("top", schema="reports", views=["base"], columns=cols["top"], tables=["public.orders"]),
        view("base", columns=cols["base"]),
    ])  # fmt: skip
    store = job(tmp_path, sources)
    assert write(tmp_path, store, contract).in_script == 2
    step = tmp_path / "step"
    assert 'CREATE SCHEMA IF NOT EXISTS "reports";' in (step / DEPLOY_FILE).read_text()

    for _ in range(2):  # the second run replaces each view with itself
        deployed = _psql(server, "-f", str(step / DEPLOY_FILE))
        assert deployed.returncode == 0, deployed.stderr

    def check():
        ran = _psql(server, "-f", str(step / CHECK_FILE), "-o", str(step / CHECK_OUTPUT))
        assert ran.returncode == 0, ran.stderr
        return read_view_check(step / CHECK_OUTPUT, contract, job_id="job1", database_name="shop",
                               contract_sha=SHA, target_database=None)  # fmt: skip

    result = check()
    assert (result.status, result.confirmed, result.mismatches) == ("pass", 2, [])
    assert SENTINEL not in (step / CHECK_OUTPUT).read_text()
    # The view keeps its check option, as on the source.
    assert server.psql("\\t on\nSELECT check_option FROM information_schema.views "
                       "WHERE table_name = 'top';").strip() == "CASCADED"  # fmt: skip
    # A routine that reads a carried view creates once the views deploy ran.
    server.psql("\\set ON_ERROR_STOP on\nSET check_function_bodies = on;\nCREATE FUNCTION "
                "public.top_count() RETURNS bigint LANGUAGE sql AS $$ SELECT count(*) FROM reports.top $$;")  # fmt: skip

    server.psql(
        "DROP VIEW reports.top CASCADE; CREATE TABLE reports.top (order_id bigint, note text);"
    )
    assert {m.view_id: m.problem for m in check().mismatches} == {"top": "not_a_view"}
    server.psql(
        "DROP TABLE reports.top; CREATE VIEW reports.top AS SELECT b.note, b.id FROM public.base b;"
    )
    assert {m.view_id: (m.problem, m.columns) for m in check().mismatches} == {
        "top": ("columns_differ", ["order_id", "note"])}  # fmt: skip
    server.psql("DROP VIEW reports.top; CREATE VIEW reports.top (order_id, note) AS "
                "SELECT b.id::int, b.note FROM public.base b;")  # fmt: skip
    assert {m.view_id: (m.problem, m.columns) for m in check().mismatches} == {
        "top": ("column_types_differ", ["order_id"])}  # fmt: skip
    server.psql("DROP VIEW reports.top;")
    assert {m.view_id: m.problem for m in check().mismatches} == {"top": "missing"}


def test_the_check_script_is_read_only(server, tmp_path):
    contract = contract_with_views([view("base")])
    store = job(tmp_path, build_routine_sources("postgresql", procedures=[], triggers=[],
                                                views=_collected(server)).sources)  # fmt: skip
    write(tmp_path, store, contract)
    script = (tmp_path / "step" / CHECK_FILE).read_text()
    # A write slipped into its transaction fails: the transaction is READ ONLY.
    poisoned = script.replace("COMMIT;", "CREATE TABLE public.not_here (id int);\nCOMMIT;")
    (tmp_path / "poisoned.sql").write_text(poisoned)
    ran = _psql(server, "-f", str(tmp_path / "poisoned.sql"))
    assert ran.returncode != 0 and "read-only transaction" in ran.stderr


@pytest.mark.parametrize(
    "definition,code",
    __import__("tests.unit.tools.sql.test_view_statement", fromlist=["PG_HOSTILE"]).PG_HOSTILE,
)
def test_a_hostile_definition_never_runs_when_the_customer_runs_the_script(
    server, tmp_path, definition, code
):
    """An edited collection file's view text (ADR-034 Decision 5): refused by code, and the
    script the customer runs drops nothing and runs no shell command."""
    from tests.unit.tools.data_movement.view_fixtures import pg_view_source

    server.psql("CREATE TABLE IF NOT EXISTS public.audit_log (msg text);")
    marker = Path("/tmp/v2082-pwned")
    marker.unlink(missing_ok=True)
    hostile = 'CREATE OR REPLACE VIEW "public"."evil" ("id") AS\n' + definition + ";"
    good = 'CREATE OR REPLACE VIEW "public"."fine" ("id") AS\n SELECT 1::bigint AS id;'
    contract = contract_with_views([view("evil"), view("fine")])
    store = job(tmp_path, [pg_view_source("evil", hostile), pg_view_source("fine", good)])
    scripts = write(tmp_path, store, contract)
    assert scripts.refused == [("evil", code)] and scripts.in_script == 1
    deploy = (tmp_path / "step" / DEPLOY_FILE).read_text()
    assert "evil" not in deploy.split("-- 1/2", 1)[-1] and "DROP" not in deploy
    ran = _psql(server, "-f", str(tmp_path / "step" / DEPLOY_FILE))
    assert ran.returncode == 0, ran.stderr
    assert server.psql("\\t on\nSELECT to_regclass('public.audit_log') IS NOT NULL;").strip() == "t"
    assert server.psql("\\t on\nSELECT to_regclass('public.orders') IS NOT NULL;").strip() == "t"
    assert server.psql("\\t on\nSELECT to_regclass('public.evil') IS NULL;").strip() == "t"
    assert not marker.exists()
    server.psql("DROP VIEW IF EXISTS public.fine;")


def test_a_target_reading_backslashes_the_old_way_runs_no_payload_and_keeps_backslashes(
    server, tmp_path
):
    """standard_conforming_strings = off at the database (#2333 review, round 2): the payload
    is refused, the script pins the setting on, so a legitimate backslash stays a backslash."""
    from tests.unit.tools.data_movement.view_fixtures import pg_view_source

    marker = Path("/tmp/v2082-pwned")
    marker.unlink(missing_ok=True)
    server.psql("CREATE TABLE IF NOT EXISTS public.audit_log (msg text);"
                "ALTER DATABASE postgres SET standard_conforming_strings = off;")  # fmt: skip
    try:
        hostile = ('CREATE OR REPLACE VIEW "public"."evil" ("a", "id") AS\n'
                   f"SELECT 'x\\' AS a, '\\! touch {marker} #' AS id;")  # fmt: skip
        legit = (
            'CREATE OR REPLACE VIEW "public"."paths" ("id") AS\n SELECT \'C:\\temp\'::text AS id;'
        )
        contract = contract_with_views([view("evil", columns=[("a", "text"), ("id", "text")]),
                                        view("paths", columns=[("id", "text")])])  # fmt: skip
        store = job(tmp_path, [pg_view_source("evil", hostile), pg_view_source("paths", legit)])
        scripts = write(tmp_path, store, contract)
        assert scripts.refused == [("evil", "statement_depends_on_backslashes")]
        deploy = tmp_path / "step" / DEPLOY_FILE
        assert "SET standard_conforming_strings = on;" in deploy.read_text()
        ran = _psql(server, "-f", str(deploy))
        assert ran.returncode == 0, ran.stderr
        assert not marker.exists()
        assert (
            server.psql("\\t on\nSELECT to_regclass('public.audit_log') IS NOT NULL;").strip()
            == "t"
        )
        value = server.psql(
            "\\set QUIET on\n\\t on\nSET standard_conforming_strings = on;\nSELECT id FROM public.paths;"
        )
        assert value.strip() == "C:\\temp"
    finally:
        server.psql("ALTER DATABASE postgres RESET standard_conforming_strings;"
                    "DROP VIEW IF EXISTS public.paths;")  # fmt: skip
        marker.unlink(missing_ok=True)
