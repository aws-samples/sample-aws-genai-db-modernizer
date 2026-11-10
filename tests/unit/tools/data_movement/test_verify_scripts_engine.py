# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's verify scripts on real engines (#1998), run as ``verify.md`` says.

PostgreSQL: one server started by ``pgserver`` on a Unix socket (no TCP listener), two
databases playing the source and Aurora. Each side gets the same data, written the way a copy
may write it (another session time zone, another numeric scale, JSON with other whitespace and
key order, another character set), and the target script runs under hostile session defaults:
both scripts must print the same counts and checksums. Then one changed row is a difference, a
source that changed between two runs is ``changing``, and a table missing on Aurora is
``missing``. The tests skip when the server cannot start. The MySQL twins need a ``mysqld``
and live in ``tests/integration/test_verify_scripts_mysql.py`` (#2091): the gated run has none.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404 -- runs the bundled psql with fixed argv
from pathlib import Path

import pytest

from src.tools.data_movement.verify_check import parse_output, read_verify
from src.tools.data_movement.verify_scripts import (
    OUTPUTS,
    SCRIPTS,
    Side,
    render_script,
    verify_tables,
)
from tests.unit.tools.data_movement.verify_fixtures import (
    HOSTILE_COLUMNS,
    HOSTILE_TABLE,
    SENTINEL,
    SHA,
    contract,
    pg_contract,
    position,
    rule,
)

# --------------------------------------------------------------------------- PostgreSQL

_PG_TABLE = """CREATE TABLE public.things (id bigint PRIMARY KEY, n numeric, m numeric(10,2),
  f8 double precision, f4 real, b boolean, ts timestamp(6), tstz timestamptz, d date, t time,
  iv interval, "by" bytea, j json, jb jsonb, arr text[], iarr integer[], tsarr timestamptz[],
  u uuid, txt text, cash money, farr double precision[]);
CREATE TABLE public.nokey (v text, k integer);
"""
_PG_SOURCE_ROWS = f"""SET TimeZone = 'UTC';
INSERT INTO public.things VALUES
 (1, 1.5, 2.50, 0.1 + 0.2, 1.1, true, '2026-01-02 03:04:05.123456', '2026-01-02 03:04:05.5+00',
  '2026-01-02', '23:59:59.999999', '1 year 2 mons 3 days 04:05:06', '\\x00ff10',
  '{{"b": 2, "a": [1, 2]}}', '{{"z": 1, "y": null}}', ARRAY['a,b', NULL, '"q"', ''],
  ARRAY[1, NULL, 3], ARRAY['2026-01-01 00:00:00+00'::timestamptz], 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11',
  'pipe | colon : {SENTINEL} é', 12.34, ARRAY[0.1 + 0.2, 1e-310]),
 (2, NULL, NULL, -0.0, 'NaN', false, '-infinity', 'infinity', '0044-03-15 BC', '00:00:00',
  '-1 days', '', '[]', '[]', '{{}}', '{{}}', NULL, NULL, '', NULL, '{{}}'),
 (3, 100, 0, 1e308, -3.4e38, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
  NULL, NULL, NULL, NULL, NULL);
INSERT INTO public.nokey VALUES ('dup', 1), ('dup', 1), (NULL, NULL), ('', 0);
CREATE TABLE public.gone (id integer PRIMARY KEY);
INSERT INTO public.gone VALUES (1);
"""
# The same values as a copy may write them: another session time zone, another scale.
_PG_TARGET_ROWS = f"""SET TimeZone = 'America/New_York';
INSERT INTO public.things VALUES
 (1, 1.500000, 2.5, 0.1 + 0.2, 1.1, true, '2026-01-02 03:04:05.123456', '2026-01-01 22:04:05.5-05',
  '2026-01-02', '23:59:59.999999', '1 year 2 mons 3 days 04:05:06', '\\x00ff10',
  '{{"a":[1,2],"b":2}}', '{{"y": null, "z": 1}}', ARRAY['a,b', NULL, '"q"', ''],
  ARRAY[1, NULL, 3], ARRAY['2025-12-31 19:00:00-05'::timestamptz], 'A0EEBC99-9C0B-4EF8-BB6D-6BB9BD380A11',
  'pipe | colon : {SENTINEL} é', 12.34, ARRAY[0.1 + 0.2, 1e-310]),
 (2, NULL, NULL, 0.0, 'NaN', false, '-infinity', 'infinity', '0044-03-15 BC', '00:00:00',
  '-1 days', '', '[]', '[]', '{{}}', '{{}}', NULL, NULL, '', NULL, '{{}}'),
 (3, 100.000, 0.00, 1e308, -3.4e38, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
  NULL, NULL, NULL, NULL, NULL, NULL);
INSERT INTO public.nokey VALUES (NULL, NULL), ('', 0), ('dup', 1), ('dup', 1);
"""
# Session defaults each side's script must override: different on each side.
_HOSTILE = {
    "source": "-c TimeZone=Asia/Tokyo -c DateStyle=SQL,DMY -c IntervalStyle=sql_standard "
              "-c extra_float_digits=0 -c bytea_output=escape",
    "target": "-c TimeZone=America/Sao_Paulo -c DateStyle=German -c IntervalStyle=iso_8601 "
              "-c extra_float_digits=-3",
}  # fmt: skip


@pytest.fixture(scope="module")
def pg(tmp_path_factory):
    pgserver = pytest.importorskip("pgserver")
    try:
        srv = pgserver.get_server(tmp_path_factory.mktemp("pgdata"), cleanup_mode="delete")
    except Exception as exc:  # no bundled server for this platform, or it cannot start
        pytest.skip(f"pgserver cannot start a PostgreSQL here: {exc}")
    srv.psql("CREATE DATABASE src;\nCREATE DATABASE tgt;\n")
    psql = Path(pgserver.__file__).parent / "pginstall" / "bin" / "psql"

    def run(database: str, *args: str, sql: str | None = None, env: dict[str, str] | None = None):
        return subprocess.run([str(psql), srv.get_uri(database=database), "-X", "-v",
                               "ON_ERROR_STOP=1", *args], input=sql, capture_output=True, text=True,
                              check=False, env={**os.environ, **(env or {})})  # fmt: skip

    for database, rows in (("src", _PG_SOURCE_ROWS), ("tgt", _PG_TARGET_ROWS)):
        done = run(database, sql=_PG_TABLE + rows)
        assert done.returncode == 0, done.stderr
    yield run
    srv.cleanup()


def _pg_output(
    pg, tmp_path: Path, side: Side, database: str, n: int = 1, c=None
) -> dict[int, tuple]:
    c = c or pg_contract()
    script = tmp_path / SCRIPTS[side]
    script.write_text(render_script(c, SHA, side))
    out = tmp_path / OUTPUTS[(side, n)]
    done = pg(database, "-f", str(script), "-o", str(out), env={"PGOPTIONS": _HOSTILE[side]})
    assert done.returncode == 0, done.stderr
    # The only error a run may print is the one table missing on Aurora.
    errors = [line for line in done.stderr.splitlines() if "ERROR" in line]
    assert len(errors) <= (0 if side == "source" else 1), done.stderr
    engine = "postgresql" if side == "source" else "aurora_postgresql"
    positions = {t.n for t in verify_tables(c)[0]}
    return parse_output(out.read_bytes(), name=out.name, side=side, engine=engine,
                        contract_sha=SHA, positions=positions).rows  # fmt: skip


@pytest.mark.passes_on_main  # only the position helper moved; PostgreSQL behaviour is unchanged
def test_the_postgresql_scripts_agree_on_the_same_data_and_name_each_kind_of_difference(
    pg, tmp_path
):
    things, nokey, gone = (
        position(pg_contract(), f"public.{t}") for t in ("things", "nokey", "gone")
    )
    source = _pg_output(pg, tmp_path, "source", "src")
    target = _pg_output(pg, tmp_path, "target", "tgt")
    assert source[things] == target[things] and source[things][0] == 3  # every tricky type
    assert source[nokey] == target[nokey] and source[nokey][0] == 4  # duplicates, any order
    assert gone in source and gone not in target  # no such table on Aurora
    assert SENTINEL not in (tmp_path / OUTPUTS[("source", 1)]).read_text()

    def statuses():
        result = read_verify(tmp_path, pg_contract(), job_id="job1", database_name="shop",
                             contract_sha=SHA)  # fmt: skip
        return {t.table_id: t.status for t in result.tables}

    assert statuses() == {
        "public.things": "match",
        "public.nokey": "match",
        "public.gone": "missing",
    }

    # One changed row on Aurora, the source unchanged across two runs: differ.
    assert pg("tgt", sql="UPDATE public.things SET txt = txt || 'x' WHERE id = 1;").returncode == 0
    target = _pg_output(pg, tmp_path, "target", "tgt")
    assert source[things][0] == target[things][0] and source[things] != target[things]
    _pg_output(pg, tmp_path, "source", "src", 2)
    _pg_output(pg, tmp_path, "target", "tgt", 2)
    assert statuses()["public.things"] == "differ"

    # The source changes between the two runs: changing, not differ.
    assert pg("src", sql="INSERT INTO public.nokey VALUES ('new', 9);").returncode == 0
    _pg_output(pg, tmp_path, "source", "src", 2)
    assert statuses()["public.nokey"] == "changing"


def test_the_postgresql_script_is_read_only(pg, tmp_path):
    script = render_script(pg_contract(), SHA, "target")
    # A write slipped into a table's transaction fails: the transaction is READ ONLY.
    head, _, tail = script.rpartition("COMMIT;")  # the last table's (gone is missing here)
    tampered = head + "CREATE TABLE public.written (id int);\nCOMMIT;" + tail
    (tmp_path / "tampered.sql").write_text(tampered)
    ran = pg("tgt", "-f", str(tmp_path / "tampered.sql"), "-o", str(tmp_path / "out.tsv"))
    assert "cannot execute CREATE TABLE in a read-only transaction" in ran.stderr
    assert pg("tgt", "-tAc", "SELECT to_regclass('public.written') IS NULL").stdout.strip() == "t"


# --------------------------------------------------------------------------- hostile names and large values


def _pg_quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def test_hostile_identifiers_reach_postgresql_intact_and_compare(pg, tmp_path):
    c = contract("aurora_postgresql", [rule("public", HOSTILE_TABLE, [(n, "text") for n in HOSTILE_COLUMNS],
                                            keyed=False)])  # fmt: skip
    cols = ", ".join(f"{_pg_quote(n)} text" for n in HOSTILE_COLUMNS)
    ddl = (f"CREATE TABLE public.{_pg_quote(HOSTILE_TABLE)} ({cols});\n"
           f"INSERT INTO public.{_pg_quote(HOSTILE_TABLE)} VALUES ('a', 'b\\!', '`');\n")  # fmt: skip
    for database in ("src", "tgt"):
        assert pg(database, sql=ddl).returncode == 0
    source = _pg_output(pg, tmp_path, "source", "src", c=c)
    target = _pg_output(pg, tmp_path, "target", "tgt", c=c)
    assert source == target and source[1][:2] == (1, 0)
    for side in ("source", "target"):
        assert "\\!" not in "".join(
            line
            for line in (tmp_path / SCRIPTS[side]).read_text().splitlines()
            if line.startswith("--")
        )
