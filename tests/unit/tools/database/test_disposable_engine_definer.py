# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A MySQL routine whose ``DEFINER`` names another account, on a disposable engine (#2426).

The backend makes that account for the run (a one-time password used for its one sign-in,
the scratch databases only), creates the routine signed in as it, locks it and drops it
with the engine, or at once when anything fails after it was made. The first
tests run on fake connections, so CI runs them; the round trips on a real ``mysqld`` are
marked ``integration``.
"""

from __future__ import annotations

import os

import pytest

from src.tools.database import disposable_engine as de

PROC = de.SourceRoutine(
    "refresh", "procedure",
    "CREATE DEFINER=`wp_user`@`localhost` PROCEDURE refresh() SELECT CURRENT_USER()",
)  # fmt: skip


class _Error(Exception):
    pass


class _Cursor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=None):
        self.conn.server.log.append((self.conn.user, sql, params))
        self.conn.last = sql

    def fetchall(self):
        if "mysql.user" in self.conn.last:
            return [(self.conn.server.existing,)]
        if "CURRENT_USER()" in self.conn.last:
            return [(self.conn.server.signed_in or f"{self.conn.user}@{self.conn.server.host}",)]
        return []

    def close(self):
        pass


class _Conn:
    def __init__(self, server, user):
        self.server, self.user, self.last, self.closed = server, user, "", False

    def cursor(self):
        return _Cursor(self)

    def close(self):
        self.closed = True


class _Server:
    password = "root-pw"  # nosec B105 - a fake  # pragma: allowlist secret
    runner_password = "runner-pw"  # nosec B105 - a fake  # pragma: allowlist secret

    def __init__(self, existing=0, reachable=True, signed_in=None):
        self.log: list[tuple[str, str, object]] = []
        self.logins: list[dict] = []
        self.existing, self.reachable, self.signed_in = existing, reachable, signed_in
        self.host = "localhost"
        self.conns: list[_Conn] = []

    def connect_args(self, user, password, database=None):
        return {"user": user, "password": password, "database": database}

    def connect(self, **kw):
        self.logins.append(kw)
        if kw["user"] not in ("root", de._RUNNER) and not self.reachable:
            raise _Error("access denied")
        conn = _Conn(self, kw["user"])
        self.conns.append(conn)
        return conn


def _backend(server):
    backend = object.__new__(de._MySQLBackend)
    backend._mc = type("mc", (), {"Error": _Error, "connect": staticmethod(server.connect)})
    backend._server = server
    backend._kinds, backend._no_run = {}, {}
    backend._made, backend._definers = [], {}
    backend._conn = _Conn(server, de._RUNNER)
    return backend


def _by(server, user):
    return [sql for who, sql, _ in server.log if who == user]


def test_a_definer_the_engine_lacks_is_made_with_the_scratch_database_only():
    server = _Server()
    assert _backend(server).load_as_definer(PROC, "wp_user", "localhost") is True
    assert _by(server, "root") == [
        de._MY_ACCOUNT_EXISTS,
        "CREATE USER `wp_user`@`localhost` IDENTIFIED BY %s",
        "GRANT ALL PRIVILEGES ON `src`.* TO `wp_user`@`localhost`",
        "ALTER USER `wp_user`@`localhost` ACCOUNT LOCK",
    ]
    made = next(p for who, sql, p in server.log if sql.startswith("CREATE USER"))
    password = made[0]
    assert isinstance(password, str) and len(password) >= 24  # bound, never in the statement
    lookup = next(p for who, sql, p in server.log if sql == de._MY_ACCOUNT_EXISTS)
    assert lookup == ["wp_user", "localhost"]  # bound, never written into the statement
    # the CREATE runs signed in as the definer, with that one-time password, never as root
    assert {"user": "wp_user", "password": password, "database": "src"} in server.logins
    assert _by(server, "wp_user") == ["SELECT CURRENT_USER()", PROC.create_sql]
    assert PROC.create_sql not in _by(server, "root") + _by(server, de._RUNNER)


def test_a_definer_already_made_for_the_run_is_reused_and_dropped_with_the_engine():
    server = _Server()
    backend = _backend(server)
    other = de.SourceRoutine("again", "procedure", PROC.create_sql.replace("refresh", "again"))
    backend.load_as_definer(PROC, "wp_user", "LOCALHOST")
    backend.load_as_definer(other, "wp_user", "localhost")  # host names ignore case
    assert _by(server, "root").count("CREATE USER `wp_user`@`LOCALHOST` IDENTIFIED BY %s") == 1
    assert sum(1 for login in server.logins if login["user"] == "wp_user") == 1
    backend._drop_definers()
    assert _by(server, "root")[-1] == "DROP USER IF EXISTS `wp_user`@`LOCALHOST`"
    assert all(c.closed for c in server.conns if c.user == "wp_user")
    assert backend._made == []


def test_a_namespace_made_later_is_granted_to_the_definer_too():
    server = _Server()
    server.host = "%"
    backend = _backend(server)
    backend.load_as_definer(PROC, "wp_user", None)  # no host: %
    backend.create_namespaces(["sales_db"])
    assert "GRANT ALL PRIVILEGES ON `sales\\_db`.* TO `wp_user`@`%`" in _by(server, "root")


def test_the_runners_own_account_loads_as_the_runner_and_makes_nothing():
    server = _Server()
    text = PROC.create_sql.replace("`wp_user`@`localhost`", "`routine_runner`@`localhost`")
    mine = de.SourceRoutine("refresh", "procedure", text)
    assert _backend(server).load_as_definer(mine, de._RUNNER, "localhost") is False
    assert _by(server, "root") == []
    assert _by(server, de._RUNNER) == [text]


@pytest.mark.parametrize(
    ("user", "host", "why"),
    [
        ("root", "localhost", "keeps for itself"),
        ("ROOT", "%", "keeps for itself"),
        ("mysql.sys", "localhost", "keeps for itself"),
        ("mysql.session", "localhost", "keeps for itself"),
        ("", "%", "anonymous"),
        ("routine_runner", "%", "runner's own user"),
        ("a\x00b", "%", "character"),
        ("app", "10.0.0.%\n", "character"),
        ("u" * 33, "%", "longer"),
        # MySQL trims an account name's leading and trailing spaces (review of #2433)
        ("root ", "localhost", "keeps for itself"),
        (" root", "localhost", "keeps for itself"),
        ("mysql.sys ", "localhost", "keeps for itself"),
        ("routine_runner ", "%", "runner's own user"),
        ("routine_runner", " localhost ", None),  # the runner's own account: loads as it
        ("\U0001f600", "%", "character"),  # outside utf8mb3, which account names use
        ("app", "\U0001f600", "character"),
        ("app", "h" * 256, "longer"),
    ],
)
def test_an_unsafe_definer_is_refused_and_nothing_is_made(user, host, why):
    if why is None:
        assert de.definer_refusal(user, host) is None
        return
    assert why in (de.definer_refusal(user, host) or "")
    server = _Server()
    with pytest.raises(de.DefinerRefused, match=why) as caught:
        _backend(server).load_as_definer(PROC, user, host)
    assert server.log == [] and server.logins == []
    if user:
        assert user not in str(caught.value)  # the reason quotes no name


@pytest.mark.parametrize(
    ("user", "host"), [("wp_user", "localhost"), ("admin", "%"), ("app`x", "%"), ("u" * 32, None)]
)
def test_an_ordinary_definer_is_not_refused(user, host):
    assert de.definer_refusal(user, host) is None


def test_a_backtick_in_a_definer_is_quoted_by_the_shared_helper():
    server = _Server()
    server.host = "%"
    _backend(server).load_as_definer(PROC, "app`x", "%")
    assert "CREATE USER `app``x`@`%` IDENTIFIED BY %s" in _by(server, "root")


def test_a_definer_is_made_under_the_name_mysql_reads_trimmed():
    server = _Server()
    _backend(server).load_as_definer(PROC, " wp_user ", " localhost ")
    assert "CREATE USER `wp_user`@`localhost` IDENTIFIED BY %s" in _by(server, "root")
    lookup = next(p for who, sql, p in server.log if sql == de._MY_ACCOUNT_EXISTS)
    assert lookup == ["wp_user", "localhost"]


def test_an_account_the_engine_already_has_is_refused():
    server = _Server(existing=1)
    with pytest.raises(de.DefinerRefused, match="already has"):
        _backend(server).load_as_definer(PROC, "wp_user", "localhost")
    assert not any(sql.startswith("CREATE USER") for sql in _by(server, "root"))


@pytest.mark.parametrize(
    "server", [_Server(reachable=False), _Server(signed_in="wp_user@%")], ids=["denied", "other"]
)
def test_an_account_a_local_sign_in_does_not_reach_is_refused_and_still_dropped(server):
    backend = _backend(server)
    with pytest.raises(de.DefinerRefused, match="local sign-in"):
        backend.load_as_definer(PROC, "wp_user", "10.0.0.%")
    assert PROC.create_sql not in [sql for _, sql, _ in server.log]
    # dropped on the spot, not left for the engine's stop
    assert _by(server, "root")[-1] == "DROP USER IF EXISTS `wp_user`@`10.0.0.%`"
    assert backend._made == [] and backend._definers == {}


def test_a_definer_on_postgresql_is_a_value_error():
    handle = de.EngineHandle("postgresql", "16", workdir=None, endpoint="", pid=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="MySQL"):
        de.load_as_definer(handle, PROC, "wp_user", "localhost")


# ---------------------------------------------------------------- MySQL (not in the gated run)

needs_mysql = pytest.mark.skipif(not de.available_majors("mysql"), reason="no mysqld binary")


def _major() -> str:
    majors = de.available_majors("mysql")
    return "8.0" if "8.0" in majors else majors[0]


def _admin(handle):
    backend = handle._backend
    return backend._mc.connect(**backend._server.connect_args("root", backend._server.password))


def _rows(handle, sql, params=()):
    conn = _admin(handle)
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        return cur.fetchall()
    finally:
        conn.close()


@pytest.mark.integration
@needs_mysql
def test_a_definer_only_original_loads_and_runs_as_its_definer_on_mysql():
    if os.geteuid() == 0:
        return
    handle = de.start("mysql", _major())
    try:
        de.load(handle, ["CREATE TABLE t (id INT PRIMARY KEY)"], {"t": [{"id": 1}]})
        with pytest.raises(de.EngineError) as plain:
            de.load(handle, (), {}, [PROC])  # main's path: the runner may not name a definer
        assert plain.value.sqlstate == "42000"
        assert de.load_as_definer(handle, PROC, "wp_user", "localhost") is True
        assert de.call(handle, "refresh", []).rows == [["wp_user@localhost"]]
        invoker = de.SourceRoutine("who", "procedure", "CREATE DEFINER=`admin`@`%` PROCEDURE "
                                   "who() SQL SECURITY INVOKER SELECT CURRENT_USER(), COUNT(*) FROM t")  # fmt: skip
        assert de.load_as_definer(handle, invoker, "admin", "%") is True
        assert de.call(handle, "who", []).rows == [["routine_runner@localhost", 1]]
        grants = [g[0] for g in _rows(handle, "SHOW GRANTS FOR 'wp_user'@'localhost'")]
        assert grants == ["GRANT USAGE ON *.* TO `wp_user`@`localhost`",
                          "GRANT ALL PRIVILEGES ON `src`.* TO `wp_user`@`localhost`"]  # fmt: skip
        locked = _rows(handle, "SELECT account_locked, authentication_string FROM mysql.user "
                       "WHERE User = 'wp_user'")  # fmt: skip
        assert len(locked) == 1 and locked[0][0] == "Y"  # locked after its one sign-in
        assert locked[0][1]  # never passwordless: a one-time password, used once
        with pytest.raises(de.DefinerRefused):
            de.load_as_definer(handle, PROC, "root", "localhost")
        handle._backend._drop_definers()
        left = _rows(handle, "SELECT User FROM mysql.user WHERE User IN ('wp_user', 'admin')")
        assert left == []
        assert de.call(handle, "refresh", []).error is not None  # its definer is gone
    finally:
        de.stop(handle)


@pytest.mark.integration
@needs_mysql
def test_a_trigger_and_an_event_load_as_their_definer_on_mysql():
    if os.geteuid() == 0:
        return
    handle = de.start("mysql", _major())
    try:
        de.load(handle, ["CREATE TABLE t (id INT PRIMARY KEY, n INT)"], {"t": [{"id": 1, "n": 0}]})
        trigger = de.SourceRoutine("bump", "trigger", "CREATE DEFINER=`admin`@`%` TRIGGER bump "
                                   "BEFORE UPDATE ON t FOR EACH ROW SET NEW.n = NEW.n + 1")  # fmt: skip
        event = de.SourceRoutine("tick", "event", "CREATE DEFINER=`admin`@`%` EVENT tick ON "
                                 "SCHEDULE EVERY 1 HOUR DO UPDATE t SET n = 10")  # fmt: skip
        assert de.load_as_definer(handle, trigger, "admin", "%") is True
        assert de.load_as_definer(handle, event, "admin", "%") is True
        assert de.execute(handle, "UPDATE t SET n = 5 WHERE id = 1").error is None
        assert de.call(handle, "tick", []).error is None
        assert de.execute(handle, "SELECT n FROM t").rows == [[11]]
    finally:
        de.stop(handle)


def _open_accounts(handle):
    """Accounts a sign-in could still use with no password, or that are not locked, apart
    from the engine's own admin and runner."""
    return _rows(handle, "SELECT User, Host FROM mysql.user WHERE User NOT IN "
                 "('root', 'routine_runner') AND (account_locked = 'N' OR "
                 "authentication_string = '')")  # fmt: skip


@pytest.mark.integration
@needs_mysql
@pytest.mark.parametrize(
    ("user", "host"),
    [("root ", "localhost"), (" root", "localhost"), ("routine_runner ", "%"),
     ("mysql.sys ", "localhost"), ("\U0001f600", "%")],
)  # fmt: skip
def test_a_padded_or_unsafe_definer_is_refused_before_any_account_on_mysql(user, host):
    if os.geteuid() == 0:
        return
    handle = de.start("mysql", _major())
    try:
        before = _rows(handle, "SELECT User, Host FROM mysql.user ORDER BY User, Host")
        with pytest.raises(de.DefinerRefused):
            de.load_as_definer(handle, PROC, user, host)
        assert _rows(handle, "SELECT User, Host FROM mysql.user ORDER BY User, Host") == before
    finally:
        de.stop(handle)


@pytest.mark.integration
@needs_mysql
def test_a_failed_sign_in_leaves_no_open_account_on_mysql():
    """An account a local sign-in does not reach is dropped at once; a routine whose CREATE
    fails leaves its account locked and with a password; neither waits for the stop."""
    if os.geteuid() == 0:
        return
    handle = de.start("mysql", _major())
    try:
        with pytest.raises(de.DefinerRefused, match="local sign-in"):
            de.load_as_definer(handle, PROC, "app", "10.0.0.%")
        assert _rows(handle, "SELECT User FROM mysql.user WHERE User = 'app'") == []
        broken = de.SourceRoutine("bad", "procedure",
                                  "CREATE DEFINER=`wp_user`@`localhost` PROCEDURE bad() SELEC 1")  # fmt: skip
        with pytest.raises(de.EngineError):
            de.load_as_definer(handle, broken, "wp_user", "localhost")
        assert _open_accounts(handle) == []
    finally:
        de.stop(handle)


@pytest.mark.passes_on_main  # a marker guard: it reads markers, not behaviour
def test_the_mysql_round_trips_are_deselected_not_skipped_in_a_gated_run():
    for test in (test_a_padded_or_unsafe_definer_is_refused_before_any_account_on_mysql,
                 test_a_failed_sign_in_leaves_no_open_account_on_mysql,
                 test_a_definer_only_original_loads_and_runs_as_its_definer_on_mysql,
                 test_a_trigger_and_an_event_load_as_their_definer_on_mysql):  # fmt: skip
        assert "integration" in {m.name for m in test.pytestmark}


def test_stopping_the_engine_drops_every_account_made_for_a_definer():
    server = _Server()
    server.stopped = False
    server.stop = lambda: setattr(server, "stopped", True)
    backend = _backend(server)
    backend.load_as_definer(PROC, "wp_user", "localhost")
    backend.close()
    assert "DROP USER IF EXISTS `wp_user`@`localhost`" in _by(server, "root")
    assert server.stopped
