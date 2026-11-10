# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's verify scripts on a real MySQL (#1998), run as ``verify.md`` says.

A throwaway ``mysqld`` on a Unix socket (no TCP, no binary log), two databases playing the
source and Aurora. Needs a ``mysqld`` and a ``mysql`` client on PATH, so it is marked
``integration``: the gated unit run has no MySQL server and turns a skip into a failure
(#2091). Run it with
``uv run pytest -m integration tests/integration/test_verify_scripts_mysql.py``. The
PostgreSQL twins stay in ``tests/unit/tools/data_movement/test_verify_scripts_engine.py``.
"""

from __future__ import annotations

import shutil
import subprocess  # nosec B404 -- runs a local mysqld and mysql client with fixed argv
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
    HOSTILE_COLUMNS,
    HOSTILE_TABLE,
    SENTINEL,
    SHA,
    contract,
    mysql_contract,
    position,
    rule,
)

pytestmark = pytest.mark.integration

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
        position(mysql_contract(), f"shop.{t}") for t in ("things", "nokey", "gone")
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


def _my_quote(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


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
