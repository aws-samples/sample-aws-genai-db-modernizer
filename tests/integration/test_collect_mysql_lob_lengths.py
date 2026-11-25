# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``collect-mysql.sql`` and the live collector measure LOB value lengths on a real server (#2128),
and name the routine privileges the session does not show (#2597).

Needs ``mysqld`` and the ``mysql`` client on PATH (the throwaway server of
``test_routine_scripts_mysql.py``). The MariaDB runs need ``mariadbd`` and ``mariadb`` on PATH, after the MySQL ones,
(a MariaDB 10.5+ install's ``bin`` folder, for example Homebrew's ``mariadb@10.5``); they skip
without it. Run it with ``uv run pytest -m integration
tests/integration/test_collect_mysql_lob_lengths.py``. The gated suite checks the script's text
and the live collector's queries in ``tests/unit/tools/database/test_mysql_lob_sizes.py``.
"""

from __future__ import annotations

import shutil
import subprocess  # nosec B404 -- runs the mysql and mariadb clients with fixed argv
import tempfile
import time
from pathlib import Path

import pytest

from src.tools.aws.ssm_executor import _parse_tabular_output
from src.tools.database.mariadb_tools import MariaDBRemoteCollector
from src.tools.database.mysql_tools import MySQLRemoteCollector
from src.tools.database.offline_collection_check import read_collection_text
from tests.integration.test_routine_scripts_mysql import _mysql, mysql_socket  # noqa: F401

pytestmark = pytest.mark.integration

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "collect-mysql.sql"

SETUP = """
    CREATE DATABASE shop;
    CREATE TABLE shop.notes (id int PRIMARY KEY, body longtext, small text, med mediumblob,
                             j json, g geometry, `we``ird` mediumtext, tail longtext, z int);
    INSERT INTO shop.notes VALUES (1, REPEAT('é', 1000), REPEAT('x', 50), REPEAT('b', 300),
                                   '{"a": 1}', ST_GeomFromText('POINT(1 2)'), 'zz', 'abc', 1),
                                  (2, NULL, NULL, NULL, NULL, ST_GeomFromText('POINT(3 4)'), NULL, NULL, NULL);
    CREATE TABLE shop.empty_t (id int PRIMARY KEY, body longtext);
    CREATE TABLE shop.big (id int PRIMARY KEY, body longtext);
    INSERT INTO shop.big VALUES (1, REPEAT('a', 3000000));
    CREATE TABLE shop.part (id int, body longtext, z int) PARTITION BY HASH(id) PARTITIONS 3;
    INSERT INTO shop.part VALUES (1, 'abc', 1), (2, 'defgh', 1), (3, REPEAT('z', 3000000), 1);
    CREATE TABLE shop.old (id int PRIMARY KEY, body longtext) ENGINE=MyISAM;
    INSERT INTO shop.old VALUES (1, 'abcd');
    CREATE USER 'reader'@'localhost'; GRANT SELECT ON shop.notes TO 'reader'@'localhost';
    GRANT SELECT ON performance_schema.* TO 'reader'@'localhost';
    GRANT SELECT ON mysql.* TO 'reader'@'localhost';
    CREATE USER 'prc'@'localhost'; GRANT SELECT ON shop.* TO 'prc'@'localhost';
    GRANT PROCESS ON *.* TO 'prc'@'localhost'; GRANT SELECT ON performance_schema.* TO 'prc'@'localhost';
    GRANT SELECT ON mysql.* TO 'prc'@'localhost';
"""  # fmt: skip
# #2367: the RDS MySQL master user holds its privileges through a role (rds_superuser_role),
# which information_schema.user_privileges does not list under the user. MySQL's syntax.
ROLE_SETUP = """
    CREATE ROLE 'admin_role'; GRANT SELECT, PROCESS ON *.* TO 'admin_role';
    CREATE USER 'admin'@'localhost'; GRANT 'admin_role' TO 'admin'@'localhost';
    SET DEFAULT ROLE ALL TO 'admin'@'localhost';
    CREATE ROLE 'shop_role'; GRANT SELECT ON shop.* TO 'shop_role';
    CREATE USER 'shopper'@'localhost'; GRANT 'shop_role' TO 'shopper'@'localhost';
    SET DEFAULT ROLE ALL TO 'shopper'@'localhost'; GRANT SELECT ON performance_schema.* TO 'shopper'@'localhost';
    CREATE USER 'cols'@'localhost'; GRANT SELECT (id, tail), INSERT ON shop.notes TO 'cols'@'localhost';
    GRANT SELECT ON shop.old TO 'cols'@'localhost'; GRANT SELECT ON performance_schema.* TO 'cols'@'localhost';
    GRANT PROCESS ON *.* TO 'cols'@'localhost';
    CREATE ROLE 'inner_role'; GRANT PROCESS ON *.* TO 'inner_role';
    CREATE ROLE 'outer_role'; GRANT 'inner_role' TO 'outer_role'; GRANT SELECT ON mysql.* TO 'outer_role';
    CREATE USER 'nest'@'localhost'; GRANT 'outer_role' TO 'nest'@'localhost';
    SET DEFAULT ROLE ALL TO 'nest'@'localhost'; GRANT SELECT ON shop.* TO 'nest'@'localhost';
    GRANT SELECT ON performance_schema.* TO 'nest'@'localhost';
    CREATE ROLE 'process_role'; GRANT PROCESS ON *.* TO 'process_role';
    CREATE USER 'nomysql'@'localhost'; GRANT 'process_role' TO 'nomysql'@'localhost';
    SET DEFAULT ROLE ALL TO 'nomysql'@'localhost'; GRANT SELECT ON shop.* TO 'nomysql'@'localhost';
    GRANT SELECT ON performance_schema.* TO 'nomysql'@'localhost';
"""  # fmt: skip

# #2597: the routine-capture hint. The RDS MySQL master user holds SHOW_ROUTINE, TRIGGER and
# EVENT through rds_superuser_role (rdsmaster); a direct grant, a role granted but not active,
# and no grant; and the role grants the grant views do not show: a role's row the user may not
# read (no SELECT on the mysql schema), a role held by the active role, a role's global grant.
_ROLE_SETUP = """
    CREATE ROLE r_routine; GRANT SELECT ON *.* TO r_routine; {show_routine}
    GRANT TRIGGER, EVENT ON shop.* TO r_routine;
    CREATE ROLE r_hidden; GRANT TRIGGER, EVENT ON shop.* TO r_hidden;
    CREATE ROLE r_inner; GRANT TRIGGER, EVENT ON shop.* TO r_inner;
    CREATE ROLE r_outer; GRANT r_inner TO r_outer; GRANT SELECT ON mysql.* TO r_outer;
    CREATE ROLE r_global; GRANT TRIGGER, EVENT ON *.* TO r_global; GRANT SELECT ON mysql.* TO r_global;
    CREATE USER 'direct'@'localhost'; GRANT SELECT ON *.* TO 'direct'@'localhost';
    GRANT TRIGGER, EVENT ON shop.* TO 'direct'@'localhost';
    CREATE USER 'lacking'@'localhost';
    CREATE USER 'inactive'@'localhost'; GRANT r_routine TO 'inactive'@'localhost';
"""
_ROLE_USERS = {"rdsmaster": "r_routine", "hidden": "r_hidden", "nested": "r_outer",
               "globalrole": "r_global"}  # fmt: skip


def _routine_setup(mariadb: bool) -> str:
    sql = _ROLE_SETUP.format(
        show_routine="" if mariadb else "GRANT SHOW_ROUTINE ON *.* TO r_routine;"
    )
    for user, role in _ROLE_USERS.items():
        account = f"'{user}'@'localhost'"
        default = (f"SET DEFAULT ROLE {role} FOR {account}" if mariadb
                   else f"SET DEFAULT ROLE ALL TO {account}")  # fmt: skip
        sql += f"CREATE USER {account}; GRANT {role} TO {account}; {default};\n"
    for user in ("lacking", "inactive", *_ROLE_USERS):  # what the script reads besides the hint
        sql += f"GRANT SELECT ON shop.* TO '{user}'@'localhost';\n"
        sql += f"GRANT SELECT ON performance_schema.* TO '{user}'@'localhost';\n"
    return sql


_ALL_THREE = ["EVENT", "SHOW_ROUTINE", "TRIGGER"]


class _Server:
    def __init__(self, client: str, sock: Path, mariadb: bool):
        self.client, self.sock, self.mariadb = client, sock, mariadb

    def run(
        self, sql: str | None = None, *args: str, user: str = "root", stdin: bytes | None = None
    ):
        argv = [self.client, "--no-defaults", f"-u{user}", f"--socket={self.sock}", *args]
        if sql is not None:
            argv += ["-e", sql]
        return subprocess.run(argv, input=stdin, capture_output=True, check=False)


@pytest.fixture(scope="module")
def mariadb_socket():
    """A throwaway MariaDB on a Unix socket (no TCP), or a skip."""
    daemon, client = shutil.which("mariadbd"), shutil.which("mariadb")
    if not daemon or not client:
        pytest.skip("no MariaDB (mariadbd, mariadb) on PATH")
    base = Path(daemon).resolve().parent.parent
    install = shutil.which("mysql_install_db", path=f"{base}/scripts") or shutil.which(
        "mariadb-install-db"
    )
    work = Path(tempfile.mkdtemp(prefix="my2128", dir="/tmp"))
    try:
        if not install or subprocess.run(
            [install, "--no-defaults", f"--basedir={base}", f"--datadir={work / 'data'}",
             "--auth-root-authentication-method=normal"], capture_output=True, check=False,
        ).returncode:  # fmt: skip
            pytest.skip("mariadb-install-db cannot initialise a data folder here")
        sock = work / "m.sock"
        proc = subprocess.Popen(
            [daemon, "--no-defaults", f"--basedir={base}", f"--datadir={work / 'data'}",
             "--skip-networking", f"--socket={sock}", f"--pid-file={work / 'm.pid'}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )  # fmt: skip
        try:
            server = _Server(client, sock, True)
            for _ in range(120):
                if sock.exists() and server.run("SELECT 1").returncode == 0:
                    break
                time.sleep(0.5)
            else:
                pytest.skip("mariadbd did not start")
            yield server
        finally:
            proc.terminate()
            proc.wait(timeout=60)
    finally:
        shutil.rmtree(work, ignore_errors=True)


@pytest.fixture(scope="module", params=["mysql", "mariadb"])
def shop(request, mysql_socket):  # noqa: F811
    if request.param == "mysql":
        client, sock = mysql_socket
        server = _Server(client, sock, False)
    else:
        server = request.getfixturevalue("mariadb_socket")
    result = server.run(
        SETUP  # nosemgrep: tainted-sql-string -- fixed setup text for a disposable test server
        + ("" if server.mariadb else ROLE_SETUP)
        + _routine_setup(server.mariadb)
    )
    assert result.returncode == 0, result.stderr
    return server


def _run(shop: _Server, user: str = "root", setting: str = ""):
    init = "SET @rate_sample_seconds=0" + (f", {setting}" if setting else "")
    if not shop.mariadb:
        init += ", SESSION information_schema_stats_expiry=0"
    return shop.run(None, "-N", "--raw", "-D", "shop", f"--init-command={init}",
                    user=user, stdin=SCRIPT.read_bytes())  # fmt: skip


def _collect(shop: _Server, user: str = "root", setting: str = "") -> dict:
    """The script's output as the ingest reads it: the collection with the sizes merged in."""
    done = _run(shop, user, setting)
    assert done.returncode == 0, done.stderr.decode()
    return read_collection_text(done.stdout.decode(), source="collection.json")


def _lengths(collection: dict) -> dict[tuple[str, str], int | None]:
    return {
        (c["table_name"], c["column_name"]): c.get("max_value_bytes")
        for c in collection["columns"]
        if c["data_type"] != "int"
    }


def test_each_lob_type_is_measured_in_bytes_and_other_text_types_are_not(shop):
    got = _lengths(_collect(shop))
    assert got[("notes", "body")] == 2000  # 1000 two-byte characters
    assert got[("notes", "med")] == 300
    assert got[("notes", "j")] == 8
    assert got[("notes", "g")] == 25  # the 4-byte SRID prefix and the WKB point
    assert got[("notes", "we`ird")] == 2  # a backtick in a column name
    assert got[("notes", "tail")] == 3  # a second column of the same table, one scan
    assert got[("notes", "small")] is None  # text is not a LOB to DMS
    assert got[("old", "body")] == 4  # a MyISAM table, sized by data_length + index_length
    if not shop.mariadb:  # an empty InnoDB table's tablespace file is not 0 bytes
        assert got[("empty_t", "body")] == 0  # measured and empty, not unmeasured


def test_a_partitioned_table_is_sized_by_all_its_partitions(shop):
    assert _lengths(_collect(shop))[("part", "body")] == 3_000_000
    # Its three partitions together are above 2 MB: summed, not each below it.
    assert _lengths(_collect(shop, setting="@lob_scan_max_mb=2"))[("part", "body")] is None


def test_a_table_above_the_limit_is_unmeasured_and_zero_turns_it_off(shop):
    assert _lengths(_collect(shop))[("big", "body")] == 3_000_000
    assert _lengths(_collect(shop, setting="@lob_scan_max_mb=2"))[("big", "body")] is None
    assert _lengths(_collect(shop, setting="@lob_scan_max_mb=2"))[("notes", "body")] == 2000
    assert set(_lengths(_collect(shop, setting="@lob_scan_max_mb=0")).values()) == {None}
    # A value that is not a whole number takes the default.
    assert _lengths(_collect(shop, setting="@lob_scan_max_mb='abc'"))[("big", "body")] == 3_000_000


def test_a_user_without_process_or_select_gets_nulls_and_the_collection_completes(shop):
    got = _lengths(_collect(shop, "reader"))
    assert ("empty_t", "body") not in got  # a table the user has no grant on is not listed
    assert got[("notes", "body")] is None  # InnoDB file size unknown without PROCESS
    assert _lengths(_collect(shop, "prc"))[("notes", "body")] == 2000
    assert _lengths(_collect(shop, "prc"))[("old", "body")] == 4


def test_a_user_whose_select_and_process_come_through_a_role_is_measured(shop):
    """#2367: the RDS master user's global SELECT and PROCESS come through a role. The script
    checks the privileges the user holds, so every LOB column is measured."""
    if shop.mariadb:
        pytest.skip("the role setup uses MySQL's syntax")
    got = _lengths(_collect(shop, "admin"))
    assert got[("notes", "body")] == 2000 and got[("notes", "tail")] == 3
    assert got[("big", "body")] == 3_000_000 and got[("old", "body")] == 4
    assert got == _lengths(_collect(shop))  # the same sizes as a direct grant gives


def test_a_select_through_a_role_or_on_some_columns_is_seen(shop):
    if shop.mariadb:
        pytest.skip("the role setup uses MySQL's syntax")
    # SELECT on the schema through a role: the MyISAM table is sized without PROCESS.
    assert _lengths(_collect(shop, "shopper"))[("old", "body")] == 4
    # SELECT on some columns only, with PROCESS so notes is sized: the selectable column is
    # read, and the column the user sees through INSERT alone is left out. Reading it would
    # stop the script with a SELECT denied error and lose every size.
    got = _lengths(_collect(shop, "cols"))
    assert got[("old", "body")] == 4
    assert got[("notes", "tail")] == 3
    assert ("notes", "body") in got and got[("notes", "body")] is None  # INSERT only


def test_process_through_a_nested_role_or_a_hidden_role_row_is_not_read(shop):
    """PROCESS through a role granted to the active role (ENABLED_ROLES lists only the active
    one), or through a role whose USER_PRIVILEGES row the user may not see (no SELECT on the
    mysql schema), is not read: the InnoDB tables stay unmeasured and the MyISAM one is sized.
    The remaining cases are #2598."""
    if shop.mariadb:
        pytest.skip("the role setup uses MySQL's syntax")
    for user in ("nest", "nomysql"):
        got = _lengths(_collect(shop, user))
        assert got[("notes", "body")] is None and got[("big", "body")] is None, user
        assert got[("old", "body")] == 4, user


def _hint(shop: _Server, user: str) -> tuple[list[str], list[str]]:
    """The routine hint as the script and as the live collector read it for ``user``."""
    script = _collect(shop, user)["metadata"]["routine_capture"]["privileges_not_seen"]
    return sorted(script), _live(shop, user).collect_routine_capture()["privileges_not_seen"]


def test_the_routine_hint_counts_a_privilege_granted_through_an_active_role(shop):
    """#2597: the script and the live collector see SHOW_ROUTINE, TRIGGER and EVENT granted
    to the session's active role, as the RDS master user holds them."""
    assert _hint(shop, "rdsmaster") == ([], [])


def test_mariadb_counts_a_role_the_active_role_holds(shop):
    """MariaDB lists the roles the active role holds as enabled too; MySQL lists only the
    active one, so its nested role's grants stay named."""
    want = [] if shop.mariadb else _ALL_THREE
    assert _hint(shop, "nested") == (want, want)


@pytest.mark.passes_on_main  # direct grants were already read; this guards them and the misses
@pytest.mark.parametrize("user", ["direct", "inactive", "lacking", "hidden"])
def test_the_routine_hint_still_reads_direct_grants_and_names_what_is_missing(shop, user):
    """A direct grant is seen. A role granted but not active, no grant, or a role's grant the
    user may not see (no SELECT on the mysql schema) leaves all three named."""
    want = [] if user == "direct" else _ALL_THREE
    assert _hint(shop, user) == (want, want)


def test_a_global_grant_to_a_role_is_seen_on_mysql_and_not_on_mariadb(shop):
    """MariaDB's USER_PRIVILEGES lists no role's global grant, so a global TRIGGER and EVENT
    through a role stay named there. SELECT on the mysql schema reads routine bodies on
    MariaDB only."""
    want = ["EVENT", "TRIGGER"] if shop.mariadb else ["SHOW_ROUTINE"]
    assert _hint(shop, "globalrole") == (want, want)


def _live(shop: _Server, user: str = "root") -> MySQLRemoteCollector:
    """The live collector, its reads run through the client as ``user``."""

    class Live(MariaDBRemoteCollector if shop.mariadb else MySQLRemoteCollector):  # type: ignore[misc]
        def __init__(self):
            self._statistics_expression = None

        def _query(self, sql: str) -> list[dict]:
            init = "" if shop.mariadb else "SET SESSION information_schema_stats_expiry=0; "
            done = shop.run(init + sql, "-B", "-D", "shop", user=user)
            assert done.returncode == 0, done.stderr.decode()
            return _parse_tabular_output(done.stdout.decode(), "mysql")

    return Live()


def test_a_timeout_costs_the_sizes_and_never_the_collection(shop):
    """A table locked by another session ends the measurement at the timeout. The client stops
    with an error and no --force, yet its output holds the whole collection: the sizes are
    printed after it, as a second object, and the ingest reads them as not measured."""
    whole = _collect(shop)
    holder = subprocess.Popen(
        [shop.client, "--no-defaults", "-uroot", f"--socket={shop.sock}", "-e",
         "LOCK TABLES shop.notes WRITE; SELECT SLEEP(6);"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        time.sleep(1.5)
        done = _run(shop, setting="@lob_scan_timeout_seconds=1")
        assert holder.poll() is None  # the run ended at its timeout, with the lock still held
        assert done.returncode != 0 and b"ERROR" in done.stderr  # the user is told
        timed_out = read_collection_text(done.stdout.decode(), source="collection.json")
    finally:
        holder.wait(timeout=30)
    # Every section of the collection is there, the same as a run without a timeout ...
    for section in ("tables", "columns", "indexes", "primary_keys", "queries", "global_stats"):
        assert len(timed_out[section]) == len(whole[section]), section
    assert timed_out["metadata"]["database_name"] == "shop"
    # ... and only the LOB sizes are missing.
    assert set(_lengths(timed_out).values()) == {None}
    assert any(v is not None for v in _lengths(whole).values())
    assert _lengths(_collect(shop))[("notes", "body")] == 2000  # the lock is gone: measured


def test_the_live_collector_measures_the_same_lengths(shop):
    class Live(MariaDBRemoteCollector if shop.mariadb else MySQLRemoteCollector):  # type: ignore[misc]
        # The client output parser drops a final row whose last field is empty, so the last
        # column of notes (z) is not listed; that is the parser's, not the measurement's.
        def __init__(self):
            self._statistics_expression = None

        def _query(self, sql: str) -> list[dict]:
            init = "" if shop.mariadb else "SET SESSION information_schema_stats_expiry=0; "
            done = shop.run(init + sql, "-B", "-D", "shop")
            assert done.returncode == 0, done.stderr.decode()
            return _parse_tabular_output(done.stdout.decode(), "mysql")

    live = Live()
    columns = live.collect_columns("notes")
    live.measure_lob_columns("notes", columns)
    got = {c["column_name"]: c.get("max_value_bytes") for c in columns}
    assert got["body"] == 2000 and got["med"] == 300 and got["tail"] == 3
    assert got["j"] == 8 and got["we`ird"] == 2 and got["small"] is None and got["id"] is None
    part = live.collect_columns("part")
    live.measure_lob_columns("part", part)
    assert [c.get("max_value_bytes") for c in part if c["column_name"] == "body"] == [3_000_000]
    live.measure_lob_columns("part", part2 := live.collect_columns("part"), lob_scan_max_mb=2)
    assert all("max_value_bytes" not in c for c in part2)
