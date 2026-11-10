# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's verify scripts on real engines (#1998), run as ``verify.md`` says.

PostgreSQL: one server started by ``pgserver`` on a Unix socket (no TCP listener), two
databases playing the source and Aurora. MySQL: a throwaway ``mysqld`` on a Unix socket, two
databases. Each side gets the same data, written the way a copy may write it (another session
time zone, another numeric scale, JSON with other whitespace and key order, another character
set), and the target script runs under hostile session defaults: both scripts must print the
same counts and checksums. Then one changed row is a difference, a source that changed between
two runs is ``changing``, and a table missing on Aurora is ``missing``. The tests skip when
the engine is not installed or cannot start.
"""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404 -- runs the bundled psql and a local mysql client with fixed argv
import tempfile
import time
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
    SHA,
    contract,
    mysql_contract,
    pg_contract,
    rule,
)

SENTINEL = "s3cr3t-v4lue"


def _position(contract, table_id: str) -> int:
    return next(t.n for t in verify_tables(contract)[0] if t.table_id == table_id)


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


def test_the_postgresql_scripts_agree_on_the_same_data_and_name_each_kind_of_difference(
    pg, tmp_path
):
    things, nokey, gone = (
        _position(pg_contract(), f"public.{t}") for t in ("things", "nokey", "gone")
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


# --------------------------------------------------------------------------- MySQL

_MY_SOURCE = f"""CREATE DATABASE shop CHARACTER SET latin1;
CREATE TABLE shop.things (id bigint PRIMARY KEY, `dec` decimal(10,2), f float, dbl double,
  flag tinyint(1), bits bit(8), dt datetime(6), ts timestamp(6) NULL, dd date, tm time(6), y year,
  c varchar(50), bl blob, j json, e enum('a','b'), vb varbinary(8)) CHARACTER SET latin1;
CREATE TABLE shop.nokey (v varchar(20), k int);
CREATE TABLE shop.gone (id int PRIMARY KEY);
SET time_zone = '+00:00';
INSERT INTO shop.things VALUES
 (1, 2.50, 1.1, 0.1 + 0.2, 1, b'10100101', '2026-01-02 03:04:05.123456', '2026-01-02 03:04:05.5',
  '2026-01-02', '-838:59:59.000000', 2026, 'pipe | colon : {SENTINEL} é', x'00ff10',
  '{{"b": 2, "a": [1, 2]}}', 'b', x'0001'),
 (2, NULL, -0.0, 1e308, 0, b'0', NULL, NULL, NULL, '00:00:00', NULL, '', x'', '[]', NULL, x''),
 (3, 0, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL);
INSERT INTO shop.nokey VALUES ('dup', 1), ('dup', 1), (NULL, NULL), ('', 0);
INSERT INTO shop.gone VALUES (1);
"""
_MY_TARGET = f"""CREATE DATABASE shop_new CHARACTER SET utf8mb4;
CREATE TABLE shop_new.things (id bigint PRIMARY KEY, `dec` decimal(10,2), f float, dbl double,
  flag tinyint(1), bits bit(8), dt datetime(6), ts timestamp(6) NULL, dd date, tm time(6), y year,
  c varchar(50), bl blob, j json, e enum('a','b'), vb varbinary(8)) CHARACTER SET utf8mb4;
CREATE TABLE shop_new.nokey (v varchar(20), k int);
SET time_zone = '-05:00';
INSERT INTO shop_new.things VALUES
 (1, 2.5, 1.1, 0.1 + 0.2, 1, b'10100101', '2026-01-02 03:04:05.123456', '2026-01-01 22:04:05.5',
  '2026-01-02', '-838:59:59', 2026, 'pipe | colon : {SENTINEL} é', x'00ff10',
  '{{"a":[1,2],"b":2}}', 'b', x'0001'),
 (2, NULL, 0.0, 1e308, 0, b'0', NULL, NULL, NULL, '00:00:00', NULL, '', x'', '[]', NULL, x''),
 (3, 0.00, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL);
INSERT INTO shop_new.nokey VALUES (NULL, NULL), ('', 0), ('dup', 1), ('dup', 1);
"""


@pytest.fixture(scope="module")
def mysql():
    """A throwaway mysqld on a Unix socket (no TCP, no binary log), or a skip."""
    mysqld, client = shutil.which("mysqld"), shutil.which("mysql")
    if not mysqld or not client:
        pytest.skip("no mysqld and mysql client on PATH")
    work = Path(tempfile.mkdtemp(prefix="my1998", dir="/tmp"))
    base = [
        mysqld,
        "--no-defaults",
        f"--datadir={work / 'data'}",
        f"--log-error={work / 'err.log'}",
    ]
    if subprocess.run(
        [*base, "--initialize-insecure"], capture_output=True, check=False
    ).returncode:
        shutil.rmtree(work, ignore_errors=True)
        pytest.skip("mysqld cannot initialise a data folder here")
    sock = work / "m.sock"
    proc = subprocess.Popen([*base, "--skip-networking", f"--socket={sock}", "--skip-log-bin",
                             "--mysqlx=OFF", f"--pid-file={work / 'm.pid'}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # fmt: skip

    def run(*args: str, sql: str | None = None, stdin: Path | None = None):
        argv = [client, "--no-defaults", "-uroot", f"--socket={sock}",
                "--default-character-set=utf8mb4", *args]  # fmt: skip
        data = stdin.read_bytes() if stdin else (sql.encode() if sql is not None else None)
        return subprocess.run(argv, input=data, capture_output=True, check=False)

    try:
        for _ in range(120):
            if sock.exists() and run(sql="SELECT 1").returncode == 0:
                break
            time.sleep(0.5)
        else:
            pytest.skip("mysqld did not start")
        for sql in (_MY_SOURCE, _MY_TARGET):
            done = run(sql=sql)
            assert done.returncode == 0, done.stderr.decode()
        yield run
    finally:
        proc.terminate()
        proc.wait(timeout=60)
        shutil.rmtree(work, ignore_errors=True)


def _my_output(mysql, tmp_path: Path, side: Side, n: int = 1, c=None) -> dict[int, tuple]:
    c = c or mysql_contract()
    script = tmp_path / SCRIPTS[side]
    script.write_text(render_script(c, SHA, side, target_database="shop_new"))
    # Session defaults the script must override: another time zone (and charset) per side.
    init = ("SET time_zone='+09:00', NAMES latin1" if side == "source"
            else "SET time_zone='-03:00'")  # fmt: skip
    ran = mysql("--batch", "--skip-column-names", "--force", f"--init-command={init}", stdin=script)
    out = tmp_path / OUTPUTS[(side, n)]
    out.write_bytes(ran.stdout)
    engine = "mysql" if side == "source" else "aurora_mysql"
    positions = {t.n for t in verify_tables(c)[0]}
    return parse_output(ran.stdout, name=out.name, side=side, engine=engine,
                        contract_sha=SHA, positions=positions).rows  # fmt: skip


def test_the_mysql_scripts_agree_on_the_same_data_and_name_each_kind_of_difference(mysql, tmp_path):
    things, nokey, gone = (
        _position(mysql_contract(), f"shop.{t}") for t in ("things", "nokey", "gone")
    )
    source = _my_output(mysql, tmp_path, "source")
    target = _my_output(mysql, tmp_path, "target")
    assert source[things] == target[things] and source[things][0] == 3
    assert source[nokey] == target[nokey] and source[nokey][0] == 4
    assert gone in source and gone not in target
    assert SENTINEL not in (tmp_path / OUTPUTS[("target", 1)]).read_text()

    def statuses():
        result = read_verify(tmp_path, mysql_contract(), job_id="job1", database_name="shop",
                             contract_sha=SHA)  # fmt: skip
        return {t.table_id: t.status for t in result.tables}

    assert statuses() == {"shop.things": "match", "shop.nokey": "match", "shop.gone": "missing"}
    assert mysql(sql="UPDATE shop_new.things SET bl = x'00ff11' WHERE id = 1;").returncode == 0
    _my_output(mysql, tmp_path, "target")  # the first run again, after the change
    _my_output(mysql, tmp_path, "source", 2)
    _my_output(mysql, tmp_path, "target", 2)
    assert statuses()["shop.things"] == "differ"
    assert mysql(sql="DELETE FROM shop.nokey WHERE v = '';").returncode == 0
    _my_output(mysql, tmp_path, "source", 2)
    assert statuses()["shop.nokey"] == "changing"


def test_the_mysql_script_is_read_only(mysql, tmp_path):
    script = render_script(mysql_contract(), SHA, "target", target_database="shop_new")
    head, _, tail = script.rpartition("COMMIT;")
    tampered = head + "INSERT INTO shop_new.nokey VALUES ('w', 1);\nCOMMIT;" + tail
    (tmp_path / "tampered.sql").write_text(tampered)
    ran = mysql("--batch", "--skip-column-names", "--force", stdin=tmp_path / "tampered.sql")
    assert b"READ ONLY transaction" in ran.stderr
    count = mysql(
        "--batch", "--skip-column-names", sql="SELECT COUNT(*) FROM shop_new.nokey WHERE v = 'w'"
    )
    assert count.stdout.strip() == b"0"


# --------------------------------------------------------------------------- hostile names and large values

# A quote, a backtick, a backslash and a client meta-command (``\!``) in a table's and its
# columns' names: they reach the server as quoted identifiers and nowhere else.
HOSTILE_TABLE = 'we"ird`t\\!x'
HOSTILE_COLUMNS = ['co"l', "b\\!c", "back`tick"]


def _pg_quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _my_quote(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


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


def test_hostile_identifiers_reach_mysql_intact_and_compare(mysql, tmp_path):
    c = contract("aurora_mysql", [rule("shop", HOSTILE_TABLE, [(n, "varchar(10)") for n in HOSTILE_COLUMNS],
                                       keyed=False)])  # fmt: skip
    cols = ", ".join(f"{_my_quote(n)} varchar(10)" for n in HOSTILE_COLUMNS)
    for database in ("shop", "shop_new"):
        table = f"{database}.{_my_quote(HOSTILE_TABLE)}"
        done = mysql(
            sql=f"CREATE TABLE {table} ({cols}); INSERT INTO {table} VALUES ('a', 'b', '`');"
        )
        assert done.returncode == 0, done.stderr.decode()
    source = _my_output(mysql, tmp_path, "source", c=c)
    target = _my_output(mysql, tmp_path, "target", c=c)
    assert source == target and source[1][:2] == (1, 0)


def test_a_mysql_lob_over_max_allowed_packet_still_compares_and_a_null_hash_is_reported(
    mysql, tmp_path
):
    """Values over the server's max_allowed_packet (a BLOB, and a latin1 text that doubles
    once converted to utf8mb4) still compare: each column is hashed on its own, so no
    intermediate value grows with the row (a CONCAT over the packet is NULL). A row hash
    forced NULL is counted, and the table is not_compared, never match."""
    c = contract("aurora_mysql", [rule("shop", "lob_blob", [("id", "int"), ("bl", "longblob")]),
                                  rule("shop", "lob_text", [("id", "int"), ("tx", "longtext")])])  # fmt: skip
    big = 1_200_000
    for database in ("shop", "shop_new"):
        done = mysql(sql=f"CREATE TABLE {database}.lob_blob (id int PRIMARY KEY, bl longblob);"
                         f"CREATE TABLE {database}.lob_text (id int PRIMARY KEY, tx longtext) CHARACTER SET latin1;"
                         f"INSERT INTO {database}.lob_blob VALUES (1, REPEAT(x'ab', {big}));"
                         f"INSERT INTO {database}.lob_text VALUES (1, REPEAT('é', {big // 2}));")  # fmt: skip
        assert done.returncode == 0, done.stderr.decode()
    assert mysql(sql="SET GLOBAL max_allowed_packet = 1048576;").returncode == 0
    try:
        blob, text = (
            next(t.n for t in verify_tables(c)[0] if t.name == n) for n in ("lob_blob", "lob_text")
        )
        source = _my_output(mysql, tmp_path, "source", c=c)
        target = _my_output(mysql, tmp_path, "target", c=c)
        for n in (blob, text):
            assert source[n] == target[n] and source[n][:2] == (1, 0)

        def read():
            result = read_verify(tmp_path, c, job_id="job1", database_name="shop", contract_sha=SHA)
            return {t.table_id: t.status for t in result.tables}

        assert read() == {"shop.lob_blob": "match", "shop.lob_text": "match"}
        assert mysql(sql="SET GLOBAL max_allowed_packet = 67108864;").returncode == 0
        assert (
            mysql(sql="UPDATE shop_new.lob_blob SET bl = INSERT(bl, 1, 1, x'00');").returncode == 0
        )
        assert mysql(sql="SET GLOBAL max_allowed_packet = 1048576;").returncode == 0
        assert _my_output(mysql, tmp_path, "target", c=c)[blob] != source[blob]

        # A row hash the server cannot build (a CONCAT over the packet) is counted, not matched.
        script = render_script(c, SHA, "target", target_database="shop_new")
        forced = script.replace("SHA2(CONCAT(", "SHA2(CONCAT(REPEAT('x', 2000000), ")
        (tmp_path / "forced.sql").write_text(forced)
        ran = mysql("--batch", "--skip-column-names", "--force", stdin=tmp_path / "forced.sql")
        (tmp_path / OUTPUTS[("target", 1)]).write_bytes(ran.stdout)
        out = parse_output(ran.stdout, name="t", side="target", engine="aurora_mysql",
                           contract_sha=SHA, positions={blob, text})  # fmt: skip
        assert out.rows[text][:2] == (1, 1) and out.rows[blob][:2] == (1, 1)
        assert read() == {"shop.lob_blob": "not_compared", "shop.lob_text": "not_compared"}
    finally:
        mysql(sql="SET GLOBAL max_allowed_packet = 67108864;")
